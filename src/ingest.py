#!/usr/bin/env python3
"""阶段 1:导入文法点清单(ingest)。

两个来源:
  * input/toc/toc_ocr.json —— 「日本語総まとめ N1 文法」目录照片的 OCR 结果(第1〜4週,74 条)
  * input/toc/n2_list.json —— JLPT N2 文法清单(200 条,由 src/fetch_n2.py 抓取)

写入 grammar_point;有英文释义的顺手写进 book_fact.source_gloss。
幂等:用 (chapter, page_ref) 做键,重复运行只 UPDATE 不插重复行。

用法:
    python3 src/ingest.py            # 导入(两个来源都导)
    python3 src/ingest.py --list     # 只打印清单给人确认,不写库
    python3 src/ingest.py --source n2
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import INPUT_TOC, ROOT, connect, init_db, log_run, now  # noqa: E402

PROMPT_VERSION = "ingest_v2"
N2_LIST = ROOT / "input" / "toc" / "n2_list.json"


def iter_n1() -> list[dict]:
    """N1 総まとめ:目录照片 OCR 的结果。"""
    toc = json.loads(INPUT_TOC.read_text(encoding="utf-8"))
    items: list[dict] = []
    order = 0
    for ch in toc["chapters"]:
        for it in ch["items"]:
            order += 1
            items.append({
                "book_order": order,
                "chapter": f"第{ch['week']}週 {ch['day']}日目",
                "title_ja": it["title_ja"].strip(),
                "reading": it.get("reading", "").strip(),
                "reading_romaji": "",
                "page_ref": str(it["page_ref"]),
                "gloss": "",
            })
    return items


def iter_n2() -> list[dict]:
    """N2:japanesetest4you 的文法清单。"""
    data = json.loads(N2_LIST.read_text(encoding="utf-8"))
    return [{
        "book_order": it["book_order"],
        "chapter": data.get("chapter", "N2文法"),
        "title_ja": it["title_ja"].strip(),
        "reading": "",
        "reading_romaji": it.get("reading_romaji", "").strip(),
        "page_ref": it["page_ref"],
        "gloss": it.get("gloss", "").strip(),
    } for it in data["items"]]


SOURCES = {"n1": iter_n1, "n2": iter_n2}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true", help="只打印清单,不写库")
    ap.add_argument("--source", choices=[*SOURCES, "all"], default="all")
    args = ap.parse_args()

    keys = list(SOURCES) if args.source == "all" else [args.source]
    items: list[dict] = []
    for k in keys:
        items += SOURCES[k]()

    if args.list:
        cur = None
        for it in items:
            if it["chapter"] != cur:
                cur = it["chapter"]
                print(f"\n== {cur}")
            print(f"  {it['book_order']:>4}. {it['title_ja']:<26} {it['reading'] or it['reading_romaji']}")
        print(f"\n合计文法点: {len(items)}")
        return 0

    started = now()
    conn = connect()
    init_db(conn)
    ok = updated = 0
    for it in items:
        # 用 (chapter, page_ref) 做幂等键:标题改了也能 UPDATE 而不是插重复行
        exists = conn.execute(
            "SELECT id FROM grammar_point WHERE chapter=? AND page_ref=?",
            (it["chapter"], it["page_ref"]),
        ).fetchone()
        if exists:
            gid = exists["id"]
            conn.execute(
                "UPDATE grammar_point SET book_order=?, title_ja=?, reading_romaji=?, page_ref=?"
                " WHERE id=?",
                (it["book_order"], it["title_ja"], it["reading_romaji"], it["page_ref"], gid),
            )
            # reading(假名)只在新行为空时写入,避免覆盖 AI 已生成的结果
            if it["reading"]:
                conn.execute(
                    "UPDATE grammar_point SET reading=? WHERE id=? AND (reading IS NULL OR reading='')",
                    (it["reading"], gid),
                )
            updated += 1
        else:
            cur = conn.execute(
                "INSERT INTO grammar_point (book_order, chapter, title_ja, reading, reading_romaji,"
                " page_ref, status) VALUES (?,?,?,?,?,?, 'new')",
                (it["book_order"], it["chapter"], it["title_ja"], it["reading"],
                 it["reading_romaji"], it["page_ref"]),
            )
            gid = cur.lastrowid
            ok += 1

        if it["gloss"]:
            row = conn.execute(
                "SELECT id FROM book_fact WHERE grammar_id=?", (gid,)).fetchone()
            if row:
                conn.execute("UPDATE book_fact SET source_gloss=? WHERE id=?",
                             (it["gloss"], row["id"]))
            else:
                conn.execute(
                    "INSERT INTO book_fact (grammar_id, source_gloss, source_image) VALUES (?,?,?)",
                    (gid, it["gloss"], it["page_ref"]),
                )
    conn.commit()
    total = conn.execute("SELECT COUNT(*) c FROM grammar_point").fetchone()["c"]
    log_run(conn, "ingest", PROMPT_VERSION, started, ok, 0,
            f"新增 {ok} / 已存在 {updated} / 库内合计 {total}")
    print(f"[ingest] 新增 {ok},已存在 {updated},库内合计 {total}")
    for ch, n in conn.execute(
            "SELECT chapter, COUNT(*) c FROM grammar_point GROUP BY chapter ORDER BY MIN(book_order)"):
        print(f"    {ch:<20} {n:>4}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
