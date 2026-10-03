#!/usr/bin/env python3
"""阶段 8:人工抽检(sample_check)

从已 verified 的文法点里随机抽 N 个,渲染成一份 markdown,方便人眼过一遍:
正面(日语)读不读得通、背面的中文意思对不对、接续有没有问题。

用法:
    python3 src/sample_check.py                 # 抽 10 个
    python3 src/sample_check.py -n 20 --seed 7  # 抽 20 个,可复现
    python3 src/sample_check.py --sqlite        # 直接读库里的 card 表,不做随机
输出:
    data/out/sample_check.md   (给人看)
    data/out/sample_check.json (带空的 verdict 字段,方便标记后回流)
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sys

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))

from common import OUT_DIR, connect, log_run  # noqa: E402

TAG_RE = re.compile(r"<[^>]+>")


def strip_html(s: str) -> str:
    """把卡片 HTML 压成给人看的纯文本(块级标签转成换行)。"""
    s = re.sub(r"<div class=['\"]lbl['\"]>([^<]*)</div>", r"\n【\1】\n", s)
    s = re.sub(r"<br\s*/?>", "\n", s)
    s = re.sub(r"</(?:div|p|li)>", "\n", s)
    s = TAG_RE.sub("", s)
    s = (s.replace("&amp;", "&").replace("&lt;", "<")
         .replace("&gt;", ">").replace("&nbsp;", " "))
    s = re.sub(r"\n{3,}", "\n\n", s)
    return "\n".join(line.strip() for line in s.splitlines()).strip()


def load_items(conn, n: int, seed: int) -> list[dict]:
    """取每个文型点最完整的一条卡(优先読解卡)做样本。"""
    rows = conn.execute(
        """
        SELECT g.id, g.book_order, g.chapter, g.title_ja, g.reading, g.status,
               c.card_type, c.front, c.back
        FROM grammar_point g
        JOIN card c ON c.grammar_id = g.id
        WHERE g.status IN ('verified', 'done')
          AND c.status IN ('draft', 'approved', 'exported')
        """
    ).fetchall()

    by_gid: dict[int, dict] = {}
    for r in rows:
        d = dict(r)
        gid = d["id"]
        # 同一文型点的多张卡里,优先読解卡(它字段最全)
        if gid not in by_gid or d["card_type"] == "reading":
            by_gid[gid] = d

    items = sorted(by_gid.values(), key=lambda d: d["book_order"])
    rnd = random.Random(seed)
    if n < len(items):
        items = rnd.sample(items, n)
    return sorted(items, key=lambda d: d["book_order"])


def render_md(items: list[dict], seed: int) -> str:
    out = [
        "# 抽检清单",
        "",
        f"共 {len(items)} 条(seed={seed})。每条请在 `[ ]` 里打勾或写问题。",
        "标记方式: `[ok]` / `[x] 意思不对:...` / `[x] 接续错:...` / `[x] 例句不自然:...` / `[x] 例句假名错:...`",
        "",
        "---",
        "",
    ]
    for i, d in enumerate(items, 1):
        out += [
            f"## {i}. #{d['id']} 「{d['title_ja']}」",
            "",
            f"- 位置: {d['chapter']} · 书中顺序 {d['book_order']}",
            f"- 卡型: `{d['card_type']}`",
            "",
            "**正面**",
            "",
            "```",
            strip_html(d["front"]),
            "```",
            "",
            "**背面**",
            "",
            "```",
            strip_html(d["back"]),
            "```",
            "",
            "- [ ] ",
            "",
            "---",
            "",
        ]
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", "--num", type=int, default=10, help="抽几条(默认 10)")
    ap.add_argument("--seed", type=int, default=0, help="随机种子,固定后可复现(默认 0)")
    args = ap.parse_args()

    conn = connect()
    items = load_items(conn, args.num, args.seed)
    if not items:
        print("[sample_check] 没有可抽的卡片,先跑 build_cards.py", file=sys.stderr)
        return 1

    md_path = OUT_DIR / "sample_check.md"
    md_path.write_text(render_md(items, args.seed), encoding="utf-8")

    json_path = OUT_DIR / "sample_check.json"
    json_path.write_text(
        json.dumps(
            {
                "seed": args.seed,
                "items": [
                    {
                        "grammar_id": d["id"],
                        "title_ja": d["title_ja"],
                        "card_type": d["card_type"],
                        "front": strip_html(d["front"]),
                        "back": strip_html(d["back"]),
                        "verdict": "",      # ok / wrong
                        "comment": "",
                        "checked_by": "",
                    }
                    for d in items
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    log_run(conn, "sample_check", None, None, len(items), 0, note=f"seed={args.seed}")
    print(f"[sample_check] 抽了 {len(items)} 条")
    print(f"  人看的: {md_path}")
    print(f"  机器读的: {json_path}")
    print(f"  条目: {', '.join('#' + str(d['id']) + ' ' + d['title_ja'] for d in items)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
