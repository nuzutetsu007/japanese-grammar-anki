#!/usr/bin/env python3
"""本地刷题网页服务:製造分野特定技能1号評価試験(電気電子機器組立て区分)。

后端只用 Python 标准库(http.server + sqlite3),前端是 quiz/ 下的原生 JS/CSS。
只绑 127.0.0.1,不联网、不用 CDN、不用外部字体。

用法:
    .venv/bin/python src/quiz_web.py                                   # 默认 db/quiz.sqlite:8765
    .venv/bin/python src/quiz_web.py --db db/quiz_test.sqlite --port 8766
然后浏览器打开 http://127.0.0.1:8765/

规矩(照项目老规矩):
  * 取题一律带「可用题口径」(见 USABLE_SQL,原样复制自 quiz_schema.sql)。
  * 每个请求单独开 sqlite 连接 → ThreadingHTTPServer 多线程也安全。
  * 练习/考试的题目接口绝不返回 answer / explanation_*(只有提交答案之后才给)。
  * 任何异常都兜住,打日志到 stderr,返回 JSON {"error": "..."} + 4xx/5xx。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
import traceback
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
QUIZ_DIR = ROOT / "quiz"
SCHEMA_SQL = ROOT / "src" / "quiz_schema.sql"
DEFAULT_DB = ROOT / "db" / "quiz.sqlite"

# 可用题统一口径(原样复制,所有取题 SQL 必须带;带别名时用 usable_sql('q'))
USABLE_SQL = ("has_figure=0 AND IFNULL(confidence,'')<>'uncertain' "
              "AND IFNULL(review_verdict,'')<>'wrong'")

KINDS = ("ox", "choice")
EXAM_MINUTES = 80           # 模拟考时长
EXAM_OX_N = 30              # 学科题数(池不足取全部)
EXAM_CHOICE_N = 10          # 実技题数(池不足取全部)
THRESH_OX = 20              # 学科合格线
THRESH_CHOICE = 6           # 実技合格线
DEFAULT_LIMIT = 20
MAX_LIMIT = 200
MAX_BODY = 1 << 20          # 请求体上限 1MB

# 静态文件白名单(防路径穿越:只认这三个路径,不做路径拼接)
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
}


# ---------------------------------------------------------------- 基础

def now() -> str:
    """仓库统一的时间写法(本地时区,ISO 风格)。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def usable_sql(alias: str = "") -> str:
    p = f"{alias}." if alias else ""
    return (f"{p}has_figure=0 AND IFNULL({p}confidence,'')<>'uncertain' "
            f"AND IFNULL({p}review_verdict,'')<>'wrong'")


class ApiError(Exception):
    """带 HTTP 状态码的接口错误。"""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


class Store:
    """题库读写:每个请求现开连接、用完就关(多线程安全)。"""

    def __init__(self, db_path: Path):
        self.db_path = db_path

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=8000")
        return conn

    def init_schema(self) -> None:
        """库/表不存在时建表(幂等);已有 question 表则什么都不做。"""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = self.connect()
        try:
            has = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='question'"
            ).fetchone()[0]
            if not has:
                conn.executescript(SCHEMA_SQL.read_text(encoding="utf-8"))
                conn.commit()
                print(f"[quiz_web] 新库:按 {SCHEMA_SQL.name} 建表 {self.db_path}", flush=True)
        finally:
            conn.close()


# ---------------------------------------------------------------- 数据小工具

def parse_choices(raw) -> list[str]:
    try:
        ch = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    if not isinstance(ch, list):
        return []
    return [str(c) for c in ch]


def question_item(r: sqlite3.Row) -> dict:
    """题目对外的字段(绝不含 answer / explanation_*)。"""
    return {
        "id": r["id"],
        "kind": r["kind"],
        "stem": r["stem"],
        "choices": parse_choices(r["choices"]),
        "chapter": r["chapter"] or "",
        "pages": r["pages"] or "",
        "ref": r["ref"] or "",
        "origin": r["origin"] or "ai",
        "difficulty": r["difficulty"] or 0,
    }


ITEM_COLS = "id, kind, stem, choices, chapter, pages, ref, origin, difficulty"


def as_int(v, name: str, lo: int, hi: int) -> int:
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise ApiError(400, f"{name} 必须是整数") from None
    if not (lo <= n <= hi):
        raise ApiError(400, f"{name} 超出范围({lo}~{hi})")
    return n


def fetch_usable(conn: sqlite3.Connection, kind: str, limit: int) -> list[sqlite3.Row]:
    return conn.execute(
        f"SELECT {ITEM_COLS} FROM question WHERE {usable_sql()} AND kind=?"
        " ORDER BY RANDOM() LIMIT ?", (kind, limit)).fetchall()


def attempt_streaks(conn: sqlite3.Connection, qids: list[int]) -> dict[int, tuple[int, int]]:
    """返回 {question_id: (连续答对次数, 累计答错次数)};只查给定的题。"""
    out: dict[int, tuple[int, int]] = {}
    if not qids:
        return out
    marks = ",".join("?" * len(qids))
    rows = conn.execute(
        f"SELECT question_id, correct FROM attempt WHERE question_id IN ({marks})"
        " AND IFNULL(chosen,'')<>''"          # 未作答(chosen='')不算错:不清零也不计数
        " ORDER BY question_id, ts, id", qids).fetchall()
    cur_qid, seq, n_wrong = None, [], 0
    for r in rows:
        if r["question_id"] != cur_qid:
            if cur_qid is not None:
                out[cur_qid] = (_streak(seq), n_wrong)
            cur_qid, seq, n_wrong = r["question_id"], [], 0
        seq.append(int(r["correct"]))
        n_wrong += 1 - int(r["correct"])
    if cur_qid is not None:
        out[cur_qid] = (_streak(seq), n_wrong)
    return out


def _streak(seq: list[int]) -> int:
    """从最后一次答题往回数连续正确次数。"""
    n = 0
    for v in reversed(seq):
        if v != 1:
            break
        n += 1
    return n


def fetch_wrong_book(conn: sqlite3.Connection) -> list[dict]:
    """错题本口径:有过答错 且 未达到「连续答对 2 次」;最近错的在前。"""
    cand = conn.execute(
        "SELECT q.id, q.kind, q.stem, q.choices, q.chapter, q.pages, q.ref, q.origin, q.difficulty,"
        " t.n_wrong, t.last_wrong FROM question q"
        " JOIN (SELECT question_id, SUM(correct=0) AS n_wrong, MAX(CASE WHEN correct=0 THEN ts END) AS last_wrong"
        "       FROM attempt WHERE IFNULL(chosen,'')<>'' GROUP BY question_id) t ON t.question_id=q.id"
        f" WHERE {usable_sql('q')}").fetchall()
    streaks = attempt_streaks(conn, [r["id"] for r in cand])
    out = []
    for r in cand:
        streak, _ = streaks.get(r["id"], (0, r["n_wrong"]))
        if streak >= 2:
            continue
        item = question_item(r)
        item["wrong_count"] = r["n_wrong"]
        item["streak"] = streak
        item["last_wrong"] = r["last_wrong"] or ""
        out.append(item)
    out.sort(key=lambda x: x["last_wrong"], reverse=True)
    return out


# ---------------------------------------------------------------- API 实现

def api_meta(store: Store, q: dict) -> dict:
    conn = store.connect()
    try:
        total = conn.execute(f"SELECT COUNT(*) FROM question WHERE {usable_sql()}").fetchone()[0]
        kinds = {k: 0 for k in KINDS}
        for r in conn.execute(f"SELECT kind, COUNT(*) n FROM question WHERE {usable_sql()} GROUP BY kind"):
            kinds[r["kind"]] = r["n"]
        chapters = [{"chapter": r["chapter"] or "", "ox": r["ox"] or 0, "choice": r["choice"] or 0}
                    for r in conn.execute(
                        "SELECT chapter, SUM(kind='ox') ox, SUM(kind='choice') choice FROM question"
                        f" WHERE {usable_sql()} GROUP BY chapter"
                        " ORDER BY (chapter=''), chapter")]
        origins = {}
        for r in conn.execute(
                f"SELECT IFNULL(origin,'') origin, COUNT(*) n FROM question WHERE {usable_sql()}"
                " GROUP BY origin ORDER BY n DESC"):
            origins[r["origin"] or "ai"] = r["n"]
        return {"usable_total": total, "kinds": kinds, "chapters": chapters, "origins": origins}
    finally:
        conn.close()


def api_questions(store: Store, q: dict) -> dict:
    kind = (q.get("kind", ["both"])[0] or "both").strip() or "both"
    if kind not in ("both",) + KINDS:
        raise ApiError(400, f"未知 kind: {kind}(只能是 ox / choice / both)")
    chapter = (q.get("chapter", [""])[0] or "").strip()
    origin = (q.get("origin", [""])[0] or "").strip()
    order = (q.get("order", ["random"])[0] or "random").strip() or "random"
    if order not in ("random", "seq"):
        raise ApiError(400, f"未知 order: {order}(只能是 random / seq)")
    limit = as_int(q.get("limit", [DEFAULT_LIMIT])[0], "limit", 1, MAX_LIMIT)

    where = [usable_sql()]
    params: list = []
    if kind != "both":
        where.append("kind=?")
        params.append(kind)
    if chapter:
        where.append("chapter=?")
        params.append(chapter)
    if origin:
        where.append("origin=?")
        params.append(origin)
    wsql = " AND ".join(where)
    order_sql = "ORDER BY RANDOM()" if order == "random" else "ORDER BY id"

    conn = store.connect()
    try:
        total = conn.execute(f"SELECT COUNT(*) FROM question WHERE {wsql}", params).fetchone()[0]
        rows = conn.execute(
            f"SELECT {ITEM_COLS} FROM question WHERE {wsql} {order_sql} LIMIT ?",
            params + [limit]).fetchall()
        return {"items": [question_item(r) for r in rows], "total": total}
    finally:
        conn.close()


def api_answer(store: Store, body: dict) -> dict:
    qid = body.get("question_id")
    if not isinstance(qid, int) or isinstance(qid, bool):
        raise ApiError(400, "question_id 必须是整数")
    chosen = body.get("chosen")
    if not isinstance(chosen, str):
        raise ApiError(400, "chosen 必须是字符串(ox 为 A/B,choice 为 A-D)")
    chosen = chosen.strip().upper()
    elapsed = body.get("elapsed_ms")
    if elapsed is not None:
        elapsed = as_int(elapsed, "elapsed_ms", 0, 24 * 3600 * 1000)

    conn = store.connect()
    try:
        row = conn.execute(
            f"SELECT id, kind, answer, explanation_zh, explanation_ja FROM question"
            f" WHERE id=? AND {usable_sql()}", (qid,)).fetchone()
        if row is None:
            raise ApiError(404, f"题目不存在或不可用: {qid}")
        allowed = ("A", "B") if row["kind"] == "ox" else ("A", "B", "C", "D")
        if chosen not in allowed:
            raise ApiError(400, f"chosen 必须是 {'/'.join(allowed)} 之一")
        correct = int(chosen == row["answer"])
        # 防重复提交:同一题同选同判 3 秒内已记过 → 不再重复 INSERT(网络重试/双击)
        dup = conn.execute(
            "SELECT 1 FROM attempt WHERE question_id=? AND chosen=? AND correct=?"
            " AND ts >= datetime('now','localtime','-3 seconds') LIMIT 1",
            (qid, chosen, correct)).fetchone()
        if dup is None:
            conn.execute(
                "INSERT INTO attempt (question_id, session_id, chosen, correct, elapsed_ms, ts)"
                " VALUES (?,NULL,?,?,?,?)", (qid, chosen, correct, elapsed, now()))
            conn.commit()
        return {"correct": bool(correct), "answer": row["answer"],
                "explanation_zh": row["explanation_zh"] or "",
                "explanation_ja": row["explanation_ja"] or ""}
    finally:
        conn.close()


def api_wrong(store: Store, q: dict) -> dict:
    conn = store.connect()
    try:
        items = fetch_wrong_book(conn)
        return {"items": items, "total": len(items)}
    finally:
        conn.close()


def _exam_remain_ms(started_at: str) -> int:
    """考试剩余毫秒(超时给负数/0);started_at 是本地时间 TEXT,格式见 now()。"""
    try:
        t0 = datetime.strptime(started_at or "", "%Y-%m-%d %H:%M:%S").timestamp()
    except (TypeError, ValueError):
        return -1       # 格式不对的当已超时处理(照回收,别把用户锁死)
    return int(EXAM_MINUTES * 60000 - (time.time() - t0) * 1000)


def _exam_load_detail(raw) -> dict:
    """session.detail 安全解析,json 坏了/不是对象一律当空。"""
    try:
        detail = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return detail if isinstance(detail, dict) else {}


def api_exam_start(store: Store, body: dict) -> dict:
    conn = store.connect()
    try:
        ox = fetch_usable(conn, "ox", EXAM_OX_N)
        choice = fetch_usable(conn, "choice", EXAM_CHOICE_N)
        qids = {"ox": [r["id"] for r in ox], "choice": [r["id"] for r in choice]}
        # 开考防呆:低于合格线意味着这场必然「挂」,别让用户白考 80 分钟
        short = []
        if len(ox) < THRESH_OX:
            short.append(f"学科(○×)现有 {len(ox)} 题,需 {THRESH_OX}")
        if len(choice) < THRESH_CHOICE:
            short.append(f"実技(4択)现有 {len(choice)} 题,需 {THRESH_CHOICE}")
        if short:
            raise ApiError(409, "可用题不足,无法开始模拟考:" + ";".join(short) + ",先去补题")
        n = len(ox) + len(choice)
        detail = {"kinds": {"ox": len(ox), "choice": len(choice)}, "qids": qids}
        # 开考互联:最近一条未完成的模拟考,没超时就不给再开;已超时的现场回收
        last = conn.execute(
            "SELECT * FROM session WHERE mode='exam' AND finished_at IS NULL"
            " ORDER BY started_at DESC, id DESC LIMIT 1").fetchone()
        if last is not None:
            if _exam_remain_ms(last["started_at"]) > 0:
                raise ApiError(
                    409, f"有一场未完成的考试(第 {last['id']} 场),"
                         "先到「模拟考」页恢复或放弃,再开始新的一场")
            conn.execute(
                "UPDATE session SET finished_at=started_at WHERE id=? AND finished_at IS NULL",
                (last["id"],))
        cur = conn.execute(
            "INSERT INTO session (mode, n, n_correct, score, passed, detail, started_at, finished_at)"
            " VALUES ('exam', ?, 0, NULL, NULL, ?, ?, NULL)",
            (n, json.dumps(detail, ensure_ascii=False), now()))
        conn.commit()
        return {"session_id": cur.lastrowid, "minutes": EXAM_MINUTES,
                "kinds": {"ox": len(ox), "choice": len(choice)},
                "ox": [question_item(r) for r in ox],
                "choice": [question_item(r) for r in choice]}
    finally:
        conn.close()


def api_exam_current(store: Store, q: dict) -> dict:
    """断点续考:查最近一条 unfinished 场次,没超时就把题目/已答原样端回去。"""
    conn = store.connect()
    try:
        row = conn.execute(
            "SELECT * FROM session WHERE mode='exam' AND finished_at IS NULL"
            " ORDER BY started_at DESC, id DESC LIMIT 1").fetchone()
        if row is None:
            return {"current": None}
        remain_ms = _exam_remain_ms(row["started_at"])
        if remain_ms <= 0:
            # 自动弃考:时间用 started_at,别污染成 now
            conn.execute(
                "UPDATE session SET finished_at=started_at WHERE id=? AND finished_at IS NULL",
                (row["id"],))
            conn.commit()
            return {"current": None}
        detail = _exam_load_detail(row["detail"])
        qids_by_kind = detail.get("qids") or {}
        items: dict[str, list] = {"ox": [], "choice": []}
        for kind in KINDS:
            want = [int(x) for x in (qids_by_kind.get(kind) or [])]
            if not want:
                continue
            marks = ",".join("?" * len(want))
            by_id = {r["id"]: r for r in conn.execute(
                f"SELECT {ITEM_COLS} FROM question WHERE id IN ({marks})", want)}
            # 按 detail 里存的顺序取(开考时的顺序),不能用 IN 的自然序
            items[kind] = [question_item(by_id[qid]) for qid in want if qid in by_id]
        # json.dumps 会把 int 键写成字符串 → 还原成 int,前端 Object.keys 才对得上题目 id
        answers = {}
        for k, v in (detail.get("answers") or {}).items():
            try:
                answers[int(k)] = v
            except (TypeError, ValueError):
                continue
        return {"current": {
            "session_id": row["id"],
            "started_at": row["started_at"] or "",
            "remain_ms": remain_ms,
            "minutes": EXAM_MINUTES,
            "kinds": detail.get("kinds") or {"ox": len(items["ox"]), "choice": len(items["choice"])},
            "answers": answers,
            "ox": items["ox"],
            "choice": items["choice"],
        }}
    finally:
        conn.close()


def api_exam_pick(store: Store, body: dict) -> dict:
    """考试过程中逐题落笔:仅改 session.detail.answers,不写 attempt。"""
    sid = body.get("session_id")
    if not isinstance(sid, int) or isinstance(sid, bool):
        raise ApiError(400, "session_id 必须是整数")
    qid = body.get("question_id")
    if not isinstance(qid, int) or isinstance(qid, bool):
        raise ApiError(400, "question_id 必须是整数")
    chosen = body.get("chosen")
    if not isinstance(chosen, str):
        raise ApiError(400, "chosen 必须是字符串(ox 为 A/B,choice 为 A-D)")
    chosen = chosen.strip().upper()

    conn = store.connect()
    try:
        # BEGIN IMMEDIATE:读改写 detail 全程握写锁,和 submit 的并发口径一致
        conn.execute("BEGIN IMMEDIATE")
        sess = conn.execute("SELECT * FROM session WHERE id=?", (sid,)).fetchone()
        if sess is not None and sess["mode"] == "exam" and not sess["finished_at"]:
            detail = _exam_load_detail(sess["detail"])
            qids_by_kind = detail.get("qids") or {}
        else:
            raise ApiError(400, "该场次已结束")
        kind_qid = {int(x): k for k in KINDS for x in (qids_by_kind.get(k) or [])}
        if qid not in kind_qid:
            raise ApiError(400, f"该题不在本场考试里: {qid}")
        qrow = conn.execute("SELECT kind FROM question WHERE id=?", (qid,)).fetchone()
        if qrow is None:
            raise ApiError(404, f"题目不存在: {qid}")
        allowed = ("A", "B") if qrow["kind"] == "ox" else ("A", "B", "C", "D")
        if chosen not in allowed:
            raise ApiError(400, f"chosen 必须是 {'/'.join(allowed)} 之一")
        answers = detail.get("answers")
        if not isinstance(answers, dict):
            answers = {}
        answers[qid] = chosen                     # int 键(json.dumps 会存成字符串,读回时转 int)
        detail["answers"] = answers
        cur = conn.execute(
            "UPDATE session SET detail=? WHERE id=? AND mode='exam' AND finished_at IS NULL",
            (json.dumps(detail, ensure_ascii=False), sid))
        if cur.rowcount == 0:                     # 读到之后刚被人交卷/弃考 → 不覆盖
            raise ApiError(400, "该场次已结束")
        conn.commit()
        return {"ok": True}
    finally:
        conn.close()


def api_exam_abandon(store: Store, body: dict) -> dict:
    """弃考:finished_at=started_at,不写 attempt、不算分。"""
    sid = body.get("session_id")
    if not isinstance(sid, int) or isinstance(sid, bool):
        raise ApiError(400, "session_id 必须是整数")
    conn = store.connect()
    try:
        cur = conn.execute(
            "UPDATE session SET finished_at=started_at"
            " WHERE id=? AND mode='exam' AND finished_at IS NULL", (sid,))
        conn.commit()
        if cur.rowcount == 0:
            raise ApiError(400, "该场次已结束")
        return {"ok": True}
    finally:
        conn.close()


def api_exam_submit(store: Store, body: dict) -> dict:
    sid = body.get("session_id")
    if not isinstance(sid, int) or isinstance(sid, bool):
        raise ApiError(400, "session_id 必须是整数")
    answers = body.get("answers")
    if answers is None:
        answers = {}
    if not isinstance(answers, dict):
        raise ApiError(400, "answers 必须是对象 {\"<题号>\": \"A\"}")
    elapsed = body.get("elapsed_ms")
    if elapsed is not None:
        elapsed = as_int(elapsed, "elapsed_ms", 0, 24 * 3600 * 1000)

    conn = store.connect()
    try:
        sess = conn.execute("SELECT * FROM session WHERE id=?", (sid,)).fetchone()
        if sess is None:
            raise ApiError(404, f"考试场次不存在: {sid}")
        if sess["mode"] != "exam":
            raise ApiError(400, f"该场次不是模拟考(mode={sess['mode']})")
        if sess["finished_at"]:
            raise ApiError(400, "该场考试已经交过卷了")

        detail_in = {}
        try:
            detail_in = json.loads(sess["detail"] or "{}")
        except ValueError:
            detail_in = {}
        if not isinstance(detail_in, dict):
            detail_in = {}
        qids_by_kind = detail_in.get("qids") or {}
        all_qids = [int(x) for k in KINDS for x in (qids_by_kind.get(k) or [])]
        rows = {}
        if all_qids:
            marks = ",".join("?" * len(all_qids))
            for r in conn.execute(
                    f"SELECT {ITEM_COLS}, answer, explanation_zh, explanation_ja FROM question"
                    f" WHERE id IN ({marks})", all_qids):
                rows[r["id"]] = r

        ts = now()
        detail_out = []
        new_att: list[tuple] = []     # attempt 只在 UPDATE 抢到场次后才写(见下)
        per_kind: dict[str, dict] = {}
        n_correct_total = 0
        for kind in KINDS:
            got = 0
            want = [int(x) for x in (qids_by_kind.get(kind) or [])]
            for qid in want:
                raw = answers.get(str(qid), "")
                chosen = raw.strip().upper() if isinstance(raw, str) else ""
                if chosen not in ("A", "B", "C", "D"):
                    chosen = ""          # 乱填/未作答一律按错算
                row = rows.get(qid)
                answer = row["answer"] if row else ""
                correct = int(bool(chosen) and chosen == answer)
                if correct:
                    got += 1
                    n_correct_total += 1
                new_att.append((qid, sid, chosen, correct, ts))
                detail_out.append({
                    "question_id": qid, "chosen": chosen, "answer": answer,
                    "correct": bool(correct),
                    "stem": row["stem"] if row else "(题目已不存在)",
                    "kind": kind,
                    "chapter": (row["chapter"] or "") if row else "",
                    "pages": (row["pages"] or "") if row else "",
                    "explanation_zh": (row["explanation_zh"] or "") if row else "",
                    "explanation_ja": (row["explanation_ja"] or "") if row else "",
                })
            # 合格线是固定常数(真题口径):池子不足时题目少,自然更难「过」——
            # 但绝不会因为「这类题一道都没有」就白送一个合格。
            thr = THRESH_OX if kind == "ox" else THRESH_CHOICE
            per_kind[kind] = {"correct": got, "total": len(want), "threshold": thr,
                              "passed": bool(got >= thr)}

        n_total = len(all_qids)
        passed = bool(per_kind["ox"]["passed"] and per_kind["choice"]["passed"])
        score = round(100.0 * n_correct_total / n_total, 1) if n_total else 0.0
        detail_db = {"kinds": per_kind, "qids": qids_by_kind, "elapsed_ms": elapsed}
        # 判重并进 UPDATE:抢到 finished_at IS NULL 的行才算本次交卷,并发双写只成一个
        conn.execute("BEGIN IMMEDIATE")     # 显式写锁:排掉并发的第二个提交者
        cur = conn.execute(
            "UPDATE session SET n=?, n_correct=?, score=?, passed=?, detail=?, finished_at=?"
            " WHERE id=? AND finished_at IS NULL",
            (n_total, n_correct_total, score, int(passed),
             json.dumps(detail_db, ensure_ascii=False), now(), sid))
        if cur.rowcount == 0:               # 已被并发请求提交 → 不写 attempt
            raise ApiError(400, "该场次已交卷")
        for t in new_att:
            conn.execute(
                "INSERT INTO attempt (question_id, session_id, chosen, correct, elapsed_ms, ts)"
                " VALUES (?,?,?,?,NULL,?)", t)
        conn.commit()
        return {"ox": per_kind["ox"], "choice": per_kind["choice"], "passed": passed,
                "ox_correct": per_kind["ox"]["correct"], "n_correct": n_correct_total,
                "score": score, "details": detail_out}
    finally:
        conn.close()


def api_stats(store: Store, q: dict) -> dict:
    conn = store.connect()
    try:
        usable_total = conn.execute(f"SELECT COUNT(*) FROM question WHERE {usable_sql()}").fetchone()[0]
        at = conn.execute("SELECT COUNT(*) n, IFNULL(SUM(correct),0) c FROM attempt").fetchone()
        n, c = at["n"], at["c"]
        by_kind = {k: {"n": 0, "correct": 0} for k in KINDS}
        for r in conn.execute(
                "SELECT q.kind k, COUNT(*) n, IFNULL(SUM(a.correct),0) c FROM attempt a"
                " JOIN question q ON q.id=a.question_id GROUP BY q.kind"):
            if r["k"] in by_kind:
                by_kind[r["k"]] = {"n": r["n"], "correct": r["c"]}
        wrong_book_size = len(fetch_wrong_book(conn))
        sessions = []
        for r in conn.execute(
                "SELECT id, started_at, finished_at, score, passed, detail FROM session"
                " ORDER BY id DESC LIMIT 10"):
            detail = None
            try:
                detail = json.loads(r["detail"]) if r["detail"] else None
            except ValueError:
                detail = r["detail"]
            sessions.append({"id": r["id"], "started_at": r["started_at"] or "",
                             "finished_at": r["finished_at"] or "",
                             "score": r["score"],
                             "passed": None if r["passed"] is None else bool(r["passed"]),
                             "detail": detail})
        return {"usable_total": usable_total,
                "attempts": {"n": n, "correct": c, "rate": round(100.0 * c / n, 1) if n else None},
                "wrong_book_size": wrong_book_size,
                "by_kind": by_kind,
                "sessions": sessions}
    finally:
        conn.close()


ROUTES = {
    ("GET", "/api/meta"): api_meta,
    ("GET", "/api/questions"): api_questions,
    ("POST", "/api/answer"): api_answer,
    ("GET", "/api/wrong"): api_wrong,
    ("POST", "/api/exam/start"): api_exam_start,
    ("GET", "/api/exam/current"): api_exam_current,
    ("POST", "/api/exam/pick"): api_exam_pick,
    ("POST", "/api/exam/abandon"): api_exam_abandon,
    ("POST", "/api/exam/submit"): api_exam_submit,
    ("GET", "/api/stats"): api_stats,
}


# ---------------------------------------------------------------- HTTP

class QuizHTTPServer(ThreadingHTTPServer):
    """带上题库句柄的 ThreadingHTTPServer(handler 通过 self.server.store 取)。"""

    daemon_threads = True

    def __init__(self, addr, store: Store):
        super().__init__(addr, QuizHandler)
        self.store = store


class QuizHandler(BaseHTTPRequestHandler):
    server_version = "QuizWeb/0.1"
    protocol_version = "HTTP/1.1"
    server: QuizHTTPServer  # 由 run_server 注入 store

    # ---- 小工具

    def log_message(self, fmt, *args):  # noqa: A003
        sys.stderr.write("[quiz_web] %s %s\n" % (self.address_string(), fmt % args))

    def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_json(self, status: int, data: dict) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _read_json(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            raise ApiError(400, "Content-Length 不合法") from None
        if n < 0 or n > MAX_BODY:
            raise ApiError(413, "请求体过大")
        if n == 0:
            return {}
        raw = self.rfile.read(n)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise ApiError(400, "请求体不是合法 JSON") from None
        if not isinstance(data, dict):
            raise ApiError(400, "请求体必须是 JSON 对象")
        return data

    # ---- 静态文件

    def _serve_static(self, path: str) -> None:
        name, ctype = STATIC_FILES[path]
        fp = QUIZ_DIR / name
        if not fp.exists():
            raise ApiError(500, f"前端文件缺失: quiz/{name}")
        self._send(200, fp.read_bytes(), ctype)

    # ---- 分发

    def _dispatch(self, method: str) -> None:
        path = urlparse(self.path).path
        if len(path) > 1:
            path = path.rstrip("/") or "/"
        try:
            if method == "GET":
                if path == "/favicon.ico":
                    self._send(204, b"", "image/x-icon")
                    return
                if path in STATIC_FILES:
                    self._serve_static(path)
                    return
            handler = ROUTES.get((method, path))
            if handler is None:
                if any(p == path for _m, p in ROUTES):      # 路径对、方法不对
                    raise ApiError(405, f"{path} 不支持 {method}")
                raise ApiError(404, f"未知路径: {method} {path}")
            body = self._read_json() if method == "POST" else {}
            query = parse_qs(urlparse(self.path).query, keep_blank_values=True)
            data = handler(self.server.store, query if method == "GET" else body)
            self._send_json(200, data)
        except ApiError as e:
            self._send_json(e.status, {"error": e.message})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:  # noqa: BLE001
            traceback.print_exc(file=sys.stderr)
            self._send_json(500, {"error": f"服务器内部错误: {type(e).__name__}: {e}"})

    def do_GET(self):    # noqa: N802
        self._dispatch("GET")

    def do_HEAD(self):   # noqa: N802
        self._dispatch("GET")

    def do_POST(self):   # noqa: N802
        self._dispatch("POST")


def run_server(store: Store, host: str, port: int) -> int:
    httpd = QuizHTTPServer((host, port), store)
    conn = store.connect()
    try:
        usable = conn.execute(f"SELECT COUNT(*) FROM question WHERE {usable_sql()}").fetchone()[0]
        by_kind = {r["kind"]: r["n"] for r in conn.execute(
            f"SELECT kind, COUNT(*) n FROM question WHERE {usable_sql()} GROUP BY kind")}
    finally:
        conn.close()
    print(f"[quiz_web] 题库: {store.db_path}(可用 {usable} 题:"
          f" ox={by_kind.get('ox', 0)} choice={by_kind.get('choice', 0)})", flush=True)
    if by_kind.get("choice", 0) < EXAM_CHOICE_N:
        print(f"[quiz_web] 注意: 実技(4択)可用题不足 {EXAM_CHOICE_N} 道,模拟考会取全部", flush=True)
    print(f"[quiz_web] 服务已启动: http://{host}:{port}/   (Ctrl-C 退出)", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[quiz_web] 收到 Ctrl-C,退出", flush=True)
    finally:
        httpd.server_close()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="特定技能刷题网页(本地服务,只用标准库)")
    ap.add_argument("--db", default=str(DEFAULT_DB), help="SQLite 库路径(默认 db/quiz.sqlite)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1")
    a = ap.parse_args()

    if a.host not in ("127.0.0.1", "localhost", "::1"):
        print(f"[quiz_web] 拒绝启动: 只允许绑本机(--host 只能是 127.0.0.1),给的是 {a.host}",
              file=sys.stderr)
        return 2
    if not (1 <= a.port <= 65535):
        print(f"[quiz_web] 端口不合法: {a.port}", file=sys.stderr)
        return 2
    if not SCHEMA_SQL.exists():
        print(f"[quiz_web] 找不到表结构文件: {SCHEMA_SQL}", file=sys.stderr)
        return 2

    store = Store(Path(a.db).resolve())
    try:
        store.init_schema()
    except sqlite3.Error as e:
        print(f"[quiz_web] 打不开题库 {a.db}: {e}", file=sys.stderr)
        return 2
    try:
        return run_server(store, a.host, a.port)
    except OSError as e:
        print(f"[quiz_web] 启动失败({a.host}:{a.port}): {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
