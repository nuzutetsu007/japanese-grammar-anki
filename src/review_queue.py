#!/usr/bin/env python3
"""阶段 5:人工审核队列(review_queue)。

只呈现:`uncertain` 内容、被任一审查者标 doubtful/wrong 的条目、多模型分歧。
每条给出:内容、各审查者意见、建议操作。

用法:
    python src/review_queue.py               # 全部待审
    python src/review_queue.py --limit 20
    python src/review_queue.py --show 5      # 展开显示前 5 条的完整内容
    python src/review_queue.py --why         # 只看「为什么被标记」(评价意见)
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import connect, init_db  # noqa: E402

# 把 expansion / example 两个来源的 review 统一成 (grammar_id, verdict, reviewer, comment)
RV = """
WITH rv AS (
  SELECT e.grammar_id AS gid, r.verdict AS verdict, r.reviewer AS reviewer, r.comment AS comment
    FROM review r
    JOIN expansion e ON r.target_table='expansion' AND r.target_id=e.id
   WHERE e.prompt_version NOT LIKE '%:superseded'
  UNION ALL
  SELECT x.grammar_id AS gid, r.verdict, r.reviewer, r.comment
    FROM review r
    JOIN example x ON r.target_table='example' AND r.target_id=x.id
   WHERE x.prompt_version NOT LIKE '%:superseded'
)
"""

QUEUE = RV + """
SELECT g.id, g.title_ja, g.chapter, g.page_ref, g.status,
       (SELECT GROUP_CONCAT(DISTINCT verdict)  FROM rv WHERE rv.gid=g.id) AS verdicts,
       (SELECT GROUP_CONCAT(DISTINCT reviewer) FROM rv WHERE rv.gid=g.id) AS reviewers,
       (SELECT COUNT(DISTINCT reviewer)        FROM rv WHERE rv.gid=g.id) AS n_reviewers,
       (SELECT COUNT(*) FROM rv WHERE rv.gid=g.id AND rv.verdict IN ('doubtful','wrong')) AS bad_cnt,
       (SELECT COUNT(*) FROM expansion e
         WHERE e.grammar_id=g.id AND e.confidence='uncertain'
           AND e.prompt_version NOT LIKE '%:superseded') AS uncertain_cnt
  FROM grammar_point g
 WHERE g.status='needs_review' OR bad_cnt > 0 OR uncertain_cnt > 0
 ORDER BY g.book_order
"""


def verdict(conn, gid: int) -> list:
    return conn.execute(
        RV + """
        SELECT reviewer, verdict, comment FROM rv WHERE gid=?
         GROUP BY reviewer, verdict, comment ORDER BY verdict DESC
        """,
        (gid,),
    ).fetchall()


def dump(conn, gid: int) -> None:
    for e in conn.execute(
        "SELECT * FROM expansion WHERE grammar_id=? AND prompt_version NOT LIKE '%:superseded'"
        " ORDER BY id",
        (gid,),
    ):
        flag = "  ⚠" if e["confidence"] in ("uncertain", "mnemonic_only") else ""
        print(f"      [{e['field']}] ({e['confidence']}){flag} {e['content']}")
    for ex in conn.execute(
        "SELECT * FROM example WHERE grammar_id=? AND prompt_version NOT LIKE '%:superseded' ORDER BY id",
        (gid,),
    ):
        print(f"      例: {ex['ja']} / {ex['reading']} / {ex['zh']}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--show", type=int, default=0, help="展开显示前 N 条完整内容")
    ap.add_argument("--why", action="store_true", help="只打印审查意见")
    args = ap.parse_args()

    conn = connect()
    init_db(conn)
    rows = conn.execute(QUEUE).fetchall()
    if args.limit:
        rows = rows[: args.limit]
    print(f"待人工审核:{len(rows)} 条\n")
    for i, r in enumerate(rows):
        print(f"{i+1:>3}. #{r['id']:>3} {r['title_ja']}  ({r['chapter']} p.{r['page_ref']}) "
              f"[{r['status']}]")
        print(f"      审查者:{r['reviewers'] or '-'}  判定:{r['verdicts'] or '-'}  "
              f"uncertain字段:{r['uncertain_cnt']}  问题条数:{r['bad_cnt']}")
        if args.why or i < args.show:
            for v in verdict(conn, r["id"]):
                mark = "✗" if v["verdict"] in ("doubtful", "wrong") else "·"
                print(f"      {mark} [{v['verdict']}] {v['reviewer']}: {v['comment']}")
        if i < args.show:
            dump(conn, r["id"])
            print()
    if not rows:
        print("(空)全部条目都已通过自检。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
