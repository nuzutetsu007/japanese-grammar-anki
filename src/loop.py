#!/usr/bin/env python3
"""常驻优化循环(loop)—— 把免费额度一直吃满,慢慢把质量磨上去。

设计要点
--------
* **可中断**:随时 Ctrl-C / kill。循环每跑完一个阶段检查停止标志,
  并且按下 `data/out/loop.stop`(或删掉不想跑的阶段)就会在下个检查点优雅退出。
* **可复联**:所有进度都在 SQLite 里(`grammar_point.status` + `loop_state`),
  进程死了、断网了、额度用完了随便重启,直接从上次的位置继续。
* **吃满免费额度**:额度由 common.py 的令牌桶 + 每日账本管,循环只管不停要活;
  没活可干就 sleep 再查(quota 是按分钟/按天重置的,重置后自动接着跑)。
* **动态优化**:每一轮按「谁的问题最多」排序处理;同一条文法点连续
  `--escalate-after` 轮没修好就升级到强模型,再不行标 stuck 交人工。
* **可观测**:状态写 `data/out/loop_status.json`(随时 cat 看),
  每轮写 `run_log`,日志在 `data/out/logs/loop.log`。

用法
----
    python3 src/loop.py                     # 一直跑(默认)
    python3 src/loop.py --once              # 只跑一轮,看看会做什么
    python3 src/loop.py --rounds 5          # 最多 5 轮
    python3 src/loop.py --workers 6 --sleep 120
    python3 src/loop.py --escalate-model groq:openai/gpt-oss-120b
"""
from __future__ import annotations

import argparse
import atexit
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import OUT_DIR, ROOT, connect, init_db, log_run, now, usage_today  # noqa: E402

STATUS_FILE = OUT_DIR / "loop_status.json"
STOP_FILE = OUT_DIR / "loop.stop"
LOCK_FILE = OUT_DIR / "loop.lock"
LOOP_LOG = OUT_DIR / "logs" / "loop.log"
PY = sys.executable

STOP = False


def _on_signal(signum, _frame):
    global STOP
    STOP = True
    print(f"\n[loop] 收到信号 {signum},跑完当前阶段就退出(进度已存库)", flush=True)


signal.signal(signal.SIGINT, _on_signal)
signal.signal(signal.SIGTERM, _on_signal)


def acquire_lock() -> bool:
    """单实例锁:两个 loop 同时跑会重复消耗配额、并发写 SQLite。

    锁文件里存 PID;若 PID 还活着就拒绝启动,进程已死则视为陈旧锁直接接管。
    """
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    if LOCK_FILE.exists():
        try:
            other = int(LOCK_FILE.read_text(encoding="utf-8").strip())
        except (ValueError, OSError):
            other = None
        if other and other != os.getpid():
            try:
                os.kill(other, 0)
            except ProcessLookupError:
                other = None                      # 进程确实不在,陈旧锁
            except PermissionError:
                pass                              # 进程存在但无权限发信号 → 仍算占用
            except OSError:
                other = None
            else:
                pass
            if other:
                print(f"[loop] 已有 loop 在运行(pid={other}),拒绝重复启动。\n"
                      f"       要停它:kill {other} 或 touch {STOP_FILE}", file=sys.stderr)
                return False
    LOCK_FILE.write_text(str(os.getpid()), encoding="utf-8")
    return True


def release_lock() -> None:
    try:
        if LOCK_FILE.exists() and LOCK_FILE.read_text(encoding="utf-8").strip() == str(os.getpid()):
            LOCK_FILE.unlink()
    except OSError:
        pass


def write_status(**kw) -> None:
    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    data: dict = {}
    if STATUS_FILE.exists():
        try:
            data = json.loads(STATUS_FILE.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            data = {}
    # 先继承旧字段,再用当前进程的 pid/时间覆盖 —— 反过来的话
    # 旧文件里的 pid 会把当前 pid 盖掉,状态永远显示上一个实例。
    data.update({"pid": os.getpid(), "updated_at": now()})
    data.update(kw)
    STATUS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def counts(conn) -> dict:
    c = {r["status"]: r["c"] for r in conn.execute(
        "SELECT status, COUNT(*) c FROM grammar_point GROUP BY status")}
    c["total"] = sum(c.values())
    c["stuck"] = conn.execute("SELECT COUNT(*) c FROM loop_state WHERE stuck=1").fetchone()["c"]
    return c


def open_issues(conn) -> int:
    """还生效的、被标 doubtful/wrong 的目标数(每个目标按 reviewer 取最新)。"""
    return conn.execute(
        """
        SELECT COUNT(DISTINCT r.target_table || ':' || r.target_id) c
          FROM review r
          LEFT JOIN expansion e ON r.target_table='expansion' AND r.target_id=e.id
          LEFT JOIN example   x ON r.target_table='example'   AND r.target_id=x.id
         WHERE r.verdict IN ('doubtful','wrong')
           AND COALESCE(e.prompt_version,'') NOT LIKE '%:superseded'
           AND COALESCE(x.prompt_version,'') NOT LIKE '%:superseded'
        """).fetchone()["c"]


def run_stage(name: str, args: list[str], extra: list[str] | None = None) -> int:
    """跑一个阶段(子进程),输出同时进日志。返回 returncode。

    关键:阶段内部**每 15 秒心跳一次**把进度写进 loop_status.json,
    不然跑十几分钟的 verify 会让你以为程序死了。
    """
    cmd = [PY, "-u", f"src/{name}.py", *args, *(extra or [])]
    LOOP_LOG.parent.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    beat = {"last": "", "done": 0}
    # stage_done/stage_last 也要显式清零:不然会从上一个阶段继承过来,
    # 让人误以为当前阶段已经有进度(和 write_status 里 pid 那个坑同源)。
    write_status(stage=name, stage_args=args, stage_started=now(),
                 stage_elapsed_sec=0, stage_done=0, stage_last="", state="running")

    def heartbeat():
        while not beat["stop"]:
            time.sleep(15)
            write_status(stage=name, stage_elapsed_sec=int(time.time() - t0),
                         stage_done=beat["done"], stage_last=beat["last"], state="running")

    beat["stop"] = False
    hb = threading.Thread(target=heartbeat, daemon=True)
    hb.start()
    print(f"\n[loop] >>> {' '.join(cmd)}", flush=True)
    with LOOP_LOG.open("a", encoding="utf-8") as log:
        log.write(f"\n\n===== {now()}  {' '.join(cmd)}\n")
        log.flush()
        proc = subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True)
        tail: list[str] = []
        for line in proc.stdout:               # type: ignore[union-attr]
            log.write(line)
            log.flush()            # 逐行落盘,否则输出量小的阶段(如 kana)
            stripped = line.rstrip()  # 会一直卡在缓冲区里,看起来像死了
            tail.append(stripped)
            if stripped:
                beat["last"] = stripped[:120]
            if stripped.startswith(("  ok", "  wrong", "  doubtful", "  ERR")):
                beat["done"] += 1
            if len(tail) > 6:
                tail.pop(0)
            if line.startswith(("  ok", "  ERR", "  wrong", "  doubtful")) is False and \
               line.strip().startswith(("[", "…")):
                print("   " + line.rstrip(), flush=True)
        proc.wait()
        beat["stop"] = True
        log.write(f"----- exit={proc.returncode}\n")
    print("   " + " | ".join(tail[-2:])[:200], flush=True)
    return proc.returncode


def should_stop() -> bool:
    return STOP or STOP_FILE.exists()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=0, help="最多几轮(0=无限)")
    ap.add_argument("--once", action="store_true", help="只跑一轮")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--sleep", type=int, default=180, help="没活可干时的休眠秒数")
    ap.add_argument("--escalate-after", type=int, default=3, help="同一文法点几轮没修好就升级模型")
    ap.add_argument("--escalate-model", default="groq:openai/gpt-oss-120b")
    ap.add_argument("--chapter", default="")
    args = ap.parse_args()

    rounds = 1 if args.once else args.rounds
    ch = ["--chapter", args.chapter] if args.chapter else []
    if not acquire_lock():
        return 1
    atexit.register(release_lock)
    conn = connect()
    init_db(conn)
    if STOP_FILE.exists():
        STOP_FILE.unlink()
    conn.execute("UPDATE loop_state SET stuck=0 WHERE stuck=1 AND grammar_id IN"
                 " (SELECT id FROM grammar_point WHERE status='verified')")
    conn.commit()

    print(f"[loop] 启动 pid={os.getpid()}  workers={args.workers} chapter={args.chapter or '全部'}",
          flush=True)

    def stage(name: str, stage_args: list[str]) -> bool:
        """跑一个阶段;成功返回 True。失败只记录,不让循环误以为已处理。"""
        rc = run_stage(name, stage_args)
        if rc != 0:
            print(f"[loop] {name} 失败 exit={rc}(本轮不计为已处理)", flush=True)
        return rc == 0

    rnd = 0
    while True:
        if rounds and rnd >= rounds:
            break
        if should_stop():
            break
        rnd += 1
        before = counts(conn)
        issues_before = open_issues(conn)
        write_status(round=rnd, counts=before, open_issues=issues_before,
                     usage=usage_today(), state="running")
        print(f"\n[loop] ===== 第 {rnd} 轮 ===== {before} 未解决意见 {issues_before}", flush=True)

        did = False          # 本输是否真的处理了东西(did 必须由「实际处理了行」决定)
        # 1) 扩写还没做的
        n = conn.execute("SELECT COUNT(*) c FROM grammar_point WHERE status='new'" if not args.chapter
                         else "SELECT COUNT(*) c FROM grammar_point WHERE status='new' AND chapter LIKE ?",
                         () if not args.chapter else (f"%{args.chapter}%",)).fetchone()["c"]
        if n:
            if stage("expand", ["--status", "new", "--workers", str(args.workers), *ch]):
                did = True
        if should_stop():
            break
        # 2) 自检
        n = conn.execute("SELECT COUNT(*) c FROM grammar_point WHERE status='expanded'" if not args.chapter
                         else "SELECT COUNT(*) c FROM grammar_point WHERE status='expanded' AND chapter LIKE ?",
                         () if not args.chapter else (f"%{args.chapter}%",)).fetchone()["c"]
        if n:
            if stage("verify", ["--workers", str(args.workers), *ch]):
                did = True
        if should_stop():
            break
        # 3) 程序化质检(不花额度)。只在「本轮开始时的未解决意见」上干活
        if issues_before:
            stage("qc", ["--dry", *ch])
            # 注音单独修:**只处理被标记过注音问题的条目**,不再每轮全量重跑
            n = conn.execute("""SELECT COUNT(*) c FROM grammar_point g WHERE g.id IN (
                    SELECT e.grammar_id FROM review r JOIN expansion e
                      ON r.target_table='expansion' AND r.target_id=e.id
                     WHERE r.verdict IN ('doubtful','wrong') AND e.prompt_version NOT LIKE '%:superseded'
                       AND (r.comment LIKE '%注音%' OR r.comment LIKE '%假名%' OR r.comment LIKE '%读音%'))""").fetchone()["c"]
            if n:
                if stage("kana", ["--flagged-only", "--skip-stuck",
                                   "--workers", str(args.workers), *ch]):
                    did = True
            if stage("qc", [*ch]):   # kana 修过的再查一遍,剩下的写进 review
                did = True
        if should_stop():
            break
        # 4) 定向修复(默认跳过 stuck)
        n = conn.execute("SELECT COUNT(*) c FROM grammar_point WHERE status='needs_review'"
                         " AND id NOT IN (SELECT grammar_id FROM loop_state WHERE stuck=1)").fetchone()["c"]
        if n:
            if stage("repair", ["--workers", str(args.workers), *ch]):
                did = True
        if should_stop():
            break

        # 5) 收敛判断 + 升级
        after = counts(conn)
        issues_after = open_issues(conn)
        improved = (after.get("verified", 0) > before.get("verified", 0)
                    or issues_after < issues_before)
        # 记录每个还没修好的文法点被尝试了几轮
        conn.execute(
            """INSERT INTO loop_state (grammar_id, tries, last_action, updated_at)
               SELECT id, 1, 'round', ? FROM grammar_point WHERE status='needs_review'
               ON CONFLICT(grammar_id) DO UPDATE SET tries = tries + 1, updated_at = ?""",
            (now(), now()))
        stubborn = [r["grammar_id"] for r in conn.execute(
            "SELECT grammar_id FROM loop_state WHERE tries >= ? AND stuck = 0"
            " AND grammar_id IN (SELECT id FROM grammar_point WHERE status='needs_review')",
            (args.escalate_after,))]
        if stubborn:
            print(f"[loop] {len(stubborn)} 条文法点连续 {args.escalate_after} 轮没修好 → "
                  f"升级到 {args.escalate_model}", flush=True)
            if stage("repair", ["--ids", ",".join(map(str, stubborn)),
                                "--workers", str(args.workers), "--model", args.escalate_model]):
                # 只有升级修复真的成功了才标 stuck;否则下轮再试
                ids = ",".join(map(str, stubborn))
                conn.execute(f"UPDATE loop_state SET stuck=1, updated_at=? WHERE grammar_id IN ({ids})",
                             (now(),))
            else:
                print("[loop] 升级修复失败,保留 stuck=0,下轮再试", flush=True)
        conn.commit()

        write_status(round=rnd, counts=after, open_issues=issues_after, usage=usage_today(),
                     improved=improved, state="idle")
        conn.execute("INSERT INTO run_log (stage, prompt_version, started_at, finished_at,"
                     " rows_ok, rows_failed, note) VALUES ('loop_round','loop_v1',?,?,?,?,?)",
                     (now(), now(), after.get("verified", 0), issues_after,
                      json.dumps(after, ensure_ascii=False)))
        conn.commit()
        print(f"[loop] 第 {rnd} 轮结束:verified={after.get('verified',0)} "
              f"needs_review={after.get('needs_review',0)} 未解决意见={issues_after}"
              f"{'  (有进展)' if improved else '  (无进展)'}", flush=True)

        if not did:
            # 真正的终止条件:没有任何待处理工作,而且没有未解决的意见
            if issues_after == 0 and after.get("needs_review", 0) == 0:
                print("[loop] 全部干净了,收工。", flush=True)
                break
            print(f"[loop] 暂时没活(可能额度用满/都在冷却),sleep {args.sleep}s 后再看。"
                  " 想停就 touch data/out/loop.stop", flush=True)
            for _ in range(args.sleep):
                if should_stop():
                    break
                time.sleep(1)
            if should_stop():
                break
        elif not improved and issues_after >= issues_before:
            # 没进展:连续这么多轮不动就把还没修好的标 stuck,防止无限震荡
            conn.execute("""UPDATE loop_state SET stuck=1, updated_at=?
                            WHERE stuck=0 AND tries >= ? AND grammar_id IN
                              (SELECT id FROM grammar_point WHERE status='needs_review')""",
                         (now(), args.escalate_after + 2))
            conn.commit()

    final = counts(conn)
    write_status(round=rnd, counts=final, open_issues=open_issues(conn),
                 usage=usage_today(), state="stopped")
    print(f"[loop] 退出。最终 {final}。随时重跑 `python3 src/loop.py` 接着跑(状态在库里)",
          flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
