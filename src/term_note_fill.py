#!/usr/bin/env python3
"""批量给 term 表填 zh(中文)/note(一句话说明)。

走 common.chat 的配额调度(groq / openrouter / google 免费层),所以基本不花钱;
输出很短(每条两句话),适合用小模型 + 小 max_tokens。

设计要点(照项目规矩):
  * **幂等/可断点续跑**:默认只处理「没有 note 或 note_version 不是当前版本」的行;
    每批写完就 commit,进程随时可以杀。
  * **绝不覆盖人工内容**:已经是当前 note_version 的行不动;`--redo` 才重做。
    重新生成 = 换 prompt 版本号(prompts/term_note_v2.md),不是原地改。
  * `uncertain` 照样入库(带标记),但按铁律不会做成卡。
  * 每批的模型名写进 `term.note_model`,方便回头知道哪批是谁写的。

用法:
    .venv/bin/python src/term_note_fill.py --category 機械・装置 --limit 40 --dry-run
    .venv/bin/python src/term_note_fill.py --category 機械・装置 --workers 3
    .venv/bin/python src/term_note_fill.py --min-star 4 --workers 4        # 全库 ★4+
    .venv/bin/python src/term_note_fill.py --redo                          # 全量重做
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import chat_json, connect, init_db, log_run, now, read_prompt   # noqa: E402

PROMPT_VERSION = "term_note_v1"
BATCH = 20
MAX_TOKENS = 2200          # 20 条 × 两句 ≈ 900 token,留余量(Groq 按声明值扣 TPM)
EXPECT_OUT = 1100
CONF_OK = {"established", "common_claim", "uncertain"}
LOCK = threading.Lock()


def collect(conn, a) -> list[dict]:
    q = ("SELECT id, term, reading, kind, category, difficulty, hits, pages FROM term"
         " WHERE 1=1")
    p: list = []
    if a.category:
        q += " AND category=?"
        p.append(a.category)
    if a.min_star:
        q += " AND difficulty>=?"
        p.append(a.min_star)
    if a.kind:
        q += " AND kind=?"
        p.append(a.kind)
    if not a.redo:                      # 只挑还没填过(或版本旧)的。
        # 注意:不能用「note 为空」来判断 —— uncertain 的行本来就是空 note,
        # 那样会被当成没填、每轮重跑。只认 note_version。
        q += " AND (note_version IS NULL OR note_version<>?)"
        p.append(PROMPT_VERSION)
    q += " ORDER BY difficulty DESC, hits, term"
    if a.limit:
        q += f" LIMIT {int(a.limit)}"
    return [dict(r) for r in conn.execute(q, p)]


def build_user_prompt(rows: list[dict]) -> str:
    lines = [f"{r['term']}|{r['reading']}|{r['category']}|{'★' * r['difficulty']}|{r['hits']}"
             for r in rows]
    return "术语(词|读音|类别|难度|出现次数):\n" + "\n".join(lines)


def valid(data, want: set[str]) -> bool:
    items = data.get("items")
    if not isinstance(items, list) or not items:
        return False
    got = {str(i.get("term", "")).strip() for i in items if isinstance(i, dict)}
    return bool(got & want) and all(
        isinstance(i, dict) and str(i.get("confidence", "")) in CONF_OK for i in items)


def clean(s: str, limit: int = 60) -> str:
    s = re.sub(r"\s+", " ", str(s or "")).strip()
    return s[:limit]


def work(rows: list[dict], system_prompt: str) -> tuple[list[dict], dict, str]:
    """线程里只调模型(不碰 DB),返回 (批, JSON, 模型名)。"""
    data = chat_json(
        [{"role": "system", "content": system_prompt},
         {"role": "user", "content": build_user_prompt(rows)}],
        max_tokens=MAX_TOKENS, expect_out=EXPECT_OUT, temperature=0.2,
        validate=lambda d: valid(d, {r["term"] for r in rows}),
    )
    return rows, data, data.get("_model", "")


def main() -> int:
    ap = argparse.ArgumentParser(description="给 term 表批量填 zh/note")
    ap.add_argument("--category", default="")
    ap.add_argument("--min-star", type=int, default=0, dest="min_star")
    ap.add_argument("--kind", default="", choices=["", "kanji", "kata"])
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--redo", action="store_true", help="连已填过的也重做(会换版本号才算数)")
    ap.add_argument("--dry-run", action="store_true", help="只打印要发出去的内容,不调模型")
    a = ap.parse_args()

    conn = connect()
    init_db(conn)
    rows = collect(conn, a)
    if not rows:
        print("[term_note_fill] 没有需要处理的术语(都填过了?加 --redo 试试)")
        return 0
    batches = [rows[i:i + BATCH] for i in range(0, len(rows), BATCH)]
    print(f"[term_note_fill] {len(rows)} 条 / {len(batches)} 批 / prompt={PROMPT_VERSION}"
          f" / workers={a.workers}")

    if a.dry_run:
        print("\n---- 样例(user prompt 前 3 批)----")
        for b in batches[:1]:
            print(build_user_prompt(b))
        print(f"\n---- 共 {len(batches)} 批,每批 ≤{BATCH} 条 ----")
        return 0

    system_prompt = read_prompt(PROMPT_VERSION)
    started = now()
    ok = bad = 0
    fails: list[str] = []
    t0 = time.time()

    def handle(res) -> None:
        nonlocal ok, bad
        rows_, data, model = res
        by_term = {r["term"]: r for r in rows_}
        written = 0
        with LOCK:
            for it in data.get("items", []):
                if not isinstance(it, dict):
                    continue
                r = by_term.get(str(it.get("term", "")).strip())
                if r is None:
                    continue                      # 模型多写的词,丢掉
                conf = str(it.get("confidence", "")).strip()
                if conf not in CONF_OK:
                    conf = "uncertain"
                conn.execute(
                    "UPDATE term SET zh=?, note=?, confidence=?, note_model=?,"
                    " note_version=?, updated_at=? WHERE id=?",
                    (clean(it.get("zh")), clean(it.get("note"), 120), conf, model,
                     PROMPT_VERSION, now(), r["id"]))
                written += 1
            conn.commit()
        ok += written
        bad += len(rows_) - written
        done = len({r["term"] for r in rows_})
        print(f"    +{written}/{done}  (model={model}, {time.time() - t0:.0f}s)", flush=True)
        if written < done:
            fails.append(f"批缺 {done - written} 条: " + ",".join(list(by_term)[:5]))

    with ThreadPoolExecutor(max_workers=max(1, a.workers)) as ex:
        futs = [ex.submit(work, b, system_prompt) for b in batches]
        for f in futs:
            try:
                handle(f.result())
            except Exception as e:  # noqa: BLE001
                bad += BATCH
                fails.append(f"{type(e).__name__}: {str(e)[:120]}")
                print(f"    [fail] {type(e).__name__}: {str(e)[:140]}", flush=True)

    log_run(conn, "term_note_fill", PROMPT_VERSION, started, ok, bad,
            "; ".join(fails)[:900])
    n = conn.execute("SELECT count(*) FROM term WHERE note_version=?", (PROMPT_VERSION,)).fetchone()[0]
    u = conn.execute("SELECT count(*) FROM term WHERE note_version=? AND confidence='uncertain'",
                     (PROMPT_VERSION,)).fetchone()[0]
    print(f"[term_note_fill] 写入 {ok} 条 / 失败 {bad} / 库里本版本共 {n}(其中 uncertain {u})"
          f" / {time.time() - t0:.0f}s")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
