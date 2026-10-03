#!/usr/bin/env python3
"""阶段 5:例句朗读音频(VOICEVOX + ffmpeg)。

给每个文法点的**前 2 条例句**生成 mp3(例子来自 example 表,顺序 = id 顺序,
和卡片「例文(接続別)」里显示的前两条一致)。

要点:
- 音色在 `SPEAKERS` 里**按例句 id 稳定随机** —— 同一句永远同一个音色,
  重跑不会换来换去;想重新洗牌加 `--reshuffle`,想固定用 `--speaker N`。
- ffmpeg 从 **stdin 管道**读 wav 直接转 mp3,**wav 全程不落盘**。
- 文件名 = `sha1(文本 + 音色 + 参数)[:16].mp3` → 内容寻址,不会重复。
- 已经有 `example.audio` 且文件还在的,默认跳过 → 幂等 / 可断点续跑。

依赖:VOICEVOX engine 在跑(默认 http://127.0.0.1:50021),系统里有 ffmpeg。

用法:
    python3 src/tts.py                     # 全量(547 条,并发 4 约 1 分钟)
    python3 src/tts.py --limit 5 --dry     # 只试 5 条,不写库
    python3 src/tts.py --ids 1,2 --speaker 9
    python3 src/tts.py --force --reshuffle # 全部重生成 + 重新随机音色
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import MEDIA_DIR, connect, init_db, log_run, now  # noqa: E402

PROMPT_VERSION = "tts_v1"

# VOICEVOX 音色(用户从试听包里挑的 3 个 —— ノーマル)
SPEAKERS = [2, 9, 3]        # 四国めたん / 波音リツ / ずんだもん
SPEAKER_NAMES = {2: "四国めたん", 3: "ずんだもん", 9: "波音リツ"}
MAX_PER_POINT = 2           # 每个文法点只做前 2 句

BASE = os.environ.get("VOICEVOX_URL", "http://127.0.0.1:50021").rstrip("/")
# 这些参数一起进文件名哈希 —— 改了参数就是新文件,不会和旧的混
AUDIO_PARAMS = {"rate": 24000, "stereo": False, "speed": 1.0, "volume": 1.0, "q": 5}


def pick_speaker(ex_id: int, forced: int = 0, reshuffle: bool = False) -> int:
    """按例句 id 稳定随机挑音色(同一个 id 永远同一个结果)。"""
    if forced:
        return forced
    rng = random.Random(f"tts:{ex_id}:{PROMPT_VERSION}" + (":re" if reshuffle else ""))
    return rng.choice(SPEAKERS)


def audio_name(text: str, speaker: int) -> str:
    key = f"{speaker}|{json.dumps(AUDIO_PARAMS, sort_keys=True)}|{text}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16] + ".mp3"


def synth_mp3(text: str, speaker: int, out: Path) -> int:
    """VOICEVOX 合成 → ffmpeg 管道转 mp3。返回 mp3 字节数。"""
    import httpx

    q = httpx.post(f"{BASE}/audio_query", params={"text": text, "speaker": speaker}, timeout=90)
    q.raise_for_status()
    aq = q.json()
    aq["outputSamplingRate"] = AUDIO_PARAMS["rate"]
    aq["outputStereo"] = AUDIO_PARAMS["stereo"]
    aq["speedScale"] = AUDIO_PARAMS["speed"]
    aq["volumeScale"] = AUDIO_PARAMS["volume"]
    w = httpx.post(f"{BASE}/synthesis", params={"speaker": speaker}, json=aq, timeout=180)
    w.raise_for_status()

    tmp = out.with_suffix(".part")
    r = subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "wav", "-i", "-",
         "-f", "mp3", "-codec:a", "libmp3lame", "-qscale:a", str(AUDIO_PARAMS["q"]),
         "-ar", str(AUDIO_PARAMS["rate"]), "-ac", "1", str(tmp)],
        input=w.content, capture_output=True)
    if r.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"ffmpeg 失败: {r.stderr.decode('utf-8', 'replace')[:200]}")
    tmp.replace(out)           # 原子落盘,中断也不会留半个文件
    return out.stat().st_size


def fetch_rows(conn, ids: list[int], limit: int) -> list:
    """每个文法点的前 MAX_PER_POINT 条例句(只取 verified/done 的活跃内容)。"""
    live = "prompt_version NOT LIKE '%:superseded'"
    # 子查询里只能用不带 e. 前缀的列名 —— 用 "e.prompt_version" 会被 SQLite
    # 当成外层相关引用,结果直接 0 行(踩过)。
    sql = f"""
      SELECT e.id, e.grammar_id, e.ja, e.audio, e.audio_speaker, g.title_ja
        FROM example e JOIN grammar_point g ON g.id = e.grammar_id
       WHERE e.{live} AND g.status IN ('verified','done')
         AND e.id IN (SELECT id FROM (
               SELECT id, ROW_NUMBER() OVER (PARTITION BY grammar_id ORDER BY id) rn
                 FROM example WHERE {live}) WHERE rn <= {MAX_PER_POINT})"""
    params: list = []
    if ids:
        sql += f" AND e.grammar_id IN ({','.join('?' * len(ids))})"
        params += ids
    sql += " ORDER BY e.grammar_id, e.id"
    if limit:
        sql += f" LIMIT {limit}"
    return conn.execute(sql, params).fetchall()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids", default="", help="只做这些文法点(逗号分隔)")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--speaker", type=int, default=0, help="固定音色 id(默认在两个里稳定随机)")
    ap.add_argument("--force", action="store_true", help="已生成的也重做")
    ap.add_argument("--reshuffle", action="store_true", help="重新随机音色")
    ap.add_argument("--dry", action="store_true", help="只统计,不合成不写库")
    ap.add_argument("--gc", action="store_true",
                    help="跑完顺手删掉没被任何活跃例句引用的 mp3(改过例句文本后的残留)")
    args = ap.parse_args()

    conn = connect()
    init_db(conn)
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)

    ids = [int(x) for x in args.ids.split(",") if x.strip()]
    rows = fetch_rows(conn, ids, args.limit)

    todo, skipped = [], 0
    for r in rows:
        if not args.force and not args.reshuffle and r["audio"] and (MEDIA_DIR / r["audio"]).exists():
            skipped += 1
            continue
        todo.append(r)

    def resolve_speaker(r) -> int:
        if args.speaker:
            return args.speaker                      # 命令行钉死
        if r["audio_speaker"] and not args.reshuffle:
            return int(r["audio_speaker"])           # 库里有记录就用回它(重跑不变)
        return pick_speaker(r["id"], 0, args.reshuffle)

    todo = [dict(r, speaker=resolve_speaker(r)) for r in todo]
    dist: dict[int, int] = {}
    for r in todo:
        dist[r["speaker"]] = dist.get(r["speaker"], 0) + 1

    print(f"[tts] 候选 {len(rows)} 条 / 待做 {len(todo)} / 已有跳过 {skipped} "
          f"(每点前 {MAX_PER_POINT} 句)")
    print("      音色分布: " + ", ".join(f"{SPEAKER_NAMES.get(k, k)}({k})×{v}"
                                        for k, v in sorted(dist.items(), key=lambda x: -x[1])))
    if args.dry:
        for r in todo[:5]:
            print(f"      [dry] ex#{r['id']} {SPEAKER_NAMES.get(r['speaker'])} ← {r['ja'][:40]}")
        return 0

    started = now()
    ok = failed = 0
    bytes_total = 0
    failures: list[str] = []
    t0 = time.time()

    def work(r):
        name = audio_name(r["ja"], r["speaker"])
        path = MEDIA_DIR / name
        if not args.force and path.exists():        # 内容寻址:同一句+同一音色已有了
            return r, name, path.stat().st_size
        return r, name, synth_mp3(r["ja"], r["speaker"], path)

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = [pool.submit(work, r) for r in todo]
        for n, fut in enumerate(as_completed(futs), 1):
            try:
                r, name, size = fut.result()
            except Exception as e:  # noqa: BLE001
                failed += 1
                failures.append(str(e)[:120])
                print(f"  ERR {type(e).__name__}: {e}", file=sys.stderr, flush=True)
                continue
            conn.execute("UPDATE example SET audio=?, audio_speaker=? WHERE id=?",
                         (name, r["speaker"], r["id"]))
            conn.commit()
            ok += 1
            bytes_total += size
            if n % 50 == 0 or n == len(futs):
                el = time.time() - t0
                print(f"  … {n}/{len(futs)}  {el:.0f}s  {bytes_total/1024/1024:.1f}MB "
                      f"({el/max(n,1)*1000:.0f}ms/条)", flush=True)

    if args.gc:
        used = {r[0] for r in conn.execute(
            "SELECT DISTINCT audio FROM example WHERE audio IS NOT NULL"
            " AND prompt_version NOT LIKE '%:superseded'")}
        freed = 0
        for f in MEDIA_DIR.glob("*.mp3"):
            if f.name not in used:
                freed += f.stat().st_size
                f.unlink()
        print(f"[tts] --gc 清理没被引用的 mp3:{freed/1024/1024:.1f}MB")

    el = time.time() - t0
    log_run(conn, "tts", PROMPT_VERSION, started, ok, failed,
            f"{len(todo)} 条 / {bytes_total/1024/1024:.1f}MB / {el:.0f}s / "
            + json.dumps({str(k): v for k, v in dist.items()}, ensure_ascii=False))
    print(f"[tts] 完成 {ok} 条,失败 {failed};共 {bytes_total/1024/1024:.1f}MB,"
          f"耗时 {el:.0f}s ({el/max(ok,1)*1000:.0f}ms/条) → {MEDIA_DIR}")
    if failures:
        print("      失败样例: " + " | ".join(failures[:3]))
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
