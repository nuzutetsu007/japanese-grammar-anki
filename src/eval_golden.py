#!/usr/bin/env python3
"""在黄金样本上对比当前库里的内容(eval/golden.json vs DB)。

用途:改完提示词后,先 `python src/expand.py --ids <golden ids> --status ... --new-version`
重跑样本,再跑本脚本看差异;通过之后才全量重跑。

用法:
    python src/eval_golden.py
    python src/eval_golden.py --full     # 连例句也逐条打印
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import ROOT, connect, init_db  # noqa: E402


def norm(s: str) -> str:
    return "".join((s or "").split())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true")
    args = ap.parse_args()

    golden = json.loads((ROOT / "eval" / "golden.json").read_text(encoding="utf-8"))
    conn = connect()
    init_db(conn)

    same = diff = 0
    for it in golden["items"]:
        gid = it["grammar_id"]
        print(f"\n=== #{gid} {it['title_ja']}   (confirmed_by={it['confirmed_by']})")
        current = {
            r["field"]: r["content"]
            for r in conn.execute(
                "SELECT * FROM expansion WHERE grammar_id=? AND prompt_version NOT LIKE '%:superseded'",
                (gid,),
            )
        }
        for field in ("connection", "meaning"):
            exp = (it["expected"] or {}).get(field)
            cur = current.get(field)
            ok = norm(exp) == norm(cur)
            same += ok
            diff += (not ok)
            print(f"  [{'= ' if ok else '≠ '}] {field}")
            if not ok:
                print(f"      golden : {exp}")
                print(f"      current: {cur}")
        if args.full:
            for ex in conn.execute(
                "SELECT * FROM example WHERE grammar_id=? AND prompt_version NOT LIKE '%:superseded'"
                " ORDER BY id",
                (gid,),
            ):
                print(f"  例: {ex['ja']} / {ex['reading']} / {ex['zh']}")

    print(f"\n[golden] 一致 {same},不一致 {diff}")
    return 0 if diff == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
