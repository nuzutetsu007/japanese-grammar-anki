#!/usr/bin/env python3
"""把 term 表(特定技能教材术语)出成 Anki 牌组。

    .venv/bin/python src/term_cards.py --dry-run          # 只看会出多少张
    .venv/bin/python src/term_cards.py                    # 出 data/out/特定技能用語.apkg
    .venv/bin/python src/term_cards.py --layout category  # 旧的:按 13 个类别分牌组

布局 tier(默认,配合 term_triage.py 的分档):
    牌组  特定技能::用語::01_必須(档1)/ 02_重要(档2)/ 03_補完(档3)
    标签  用語 / 用語::難度::N        (v3:类别、優先标签已去掉,精简)
    剔除词(status=skip 且没发过)带 用語::削除候補 进 00_削除,
    随包发一次后 skip_sent=1,以后不再发。

v3 起卡片模板不再显示类别。交付口径 = 「删掉旧牌组 → 全新导入」的干净流程。
(教训:Anki 实测重导入只原地更新字段/标签、保留进度,**不会**迁牌组;
中途换布局就得手动按标签改牌组,方案已放弃。)

只出 confidence=established / common_claim 的;uncertain(抽取噪声)一律不出。
guid 由 guid_key(默认 "term:reading")稳定生成 → 同库重导入是**更新**而不是重复。
出完把 status 记成 carded;含剔除词的那次会把 skip_sent 置 1。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import MEDIA_DIR, OUT_DIR, connect, init_db, log_run, now   # noqa: E402

import genanki                                              # noqa: E402

EXPORT_VERSION = "term_cards_v3"
NOTE_TYPE = "特定技能-用語"
MODEL_ID = int(hashlib.sha1(b"term-cards:model:vocab").hexdigest()[:8], 16)
DECK_ROOT = "特定技能::用語"
TIER_DECKS = {
    "1": f"{DECK_ROOT}::01_必須",
    "2": f"{DECK_ROOT}::02_重要",
    "3": f"{DECK_ROOT}::03_補完",
}
SKIP_DECK = f"{DECK_ROOT}::00_削除"

CSS = """
.card { font-family: "Hiragino Sans", "Noto Sans JP", sans-serif; font-size: 20px;
        text-align: center; color: #22303c; background: #fbfcfd; }
.term { font-size: 40px; font-weight: 700; margin: .5em 0; }
.rd   { color: #5b6b7a; font-size: 20px; }
.zh   { font-size: 30px; font-weight: 700; color: #0b6b52; margin: .3em 0; }
.note { text-align: left; line-height: 1.7; background: #eef3f6; border-radius: 10px;
        padding: .7em .9em; margin: .8em auto 0; max-width: 34em; }
.meta { color: #8794a1; font-size: 13px; margin-top: 1em; }
.tag  { display: inline-block; padding: .1em .55em; border-radius: 999px;
        background: #e6eef4; color: #3b5163; font-size: 13px; font-weight: 700; }
hr#answer { border: none; border-top: 1px solid #dde5ea; margin: 1em 0; }
"""

MODEL = genanki.Model(
    MODEL_ID, NOTE_TYPE,
    fields=[{"name": f} for f in
            ["Term", "Reading", "Zh", "Note", "Category", "Difficulty", "Pages",
             "Audio"]],
    templates=[{
        "name": "用語",
        "qfmt": '<div class="term">{{Term}}</div>',
        "afmt": '<hr id="answer">\n'
                '<div class="term">{{Term}}{{#Reading}}'
                '<span class="rd">　{{Reading}}</span>{{/Reading}}</div>\n'
                '{{Audio}}\n'
                '<div class="zh">{{Zh}}</div>\n'
                '<div class="note">{{Note}}</div>\n'
                '<div class="meta">{{Difficulty}}　原書 p.{{Pages}}</div>',
    }],
    css=CSS,
)


def guid(term: str, reading: str, guid_key: str | None = None) -> str:
    """guid_key(首发出卡时的 "term:reading")优先 —— 改名/改读音后 GUID 不变。"""
    key = guid_key or f"{term}:{reading}"
    return hashlib.sha1(f"term-vocab:{key}".encode()).hexdigest()[:16]


def deck_name(category: str) -> str:
    return f"{DECK_ROOT}::{category or 'その他'}"


def deck_id(name: str) -> int:
    return int(hashlib.sha1(f"term-vocab:deck:{name}".encode()).hexdigest()[:8], 16)


def tags_for(r) -> list[str]:
    tags = ["用語", f"用語::難度::{r['difficulty']}"]   # v3:类别/優先标签已去掉
    if r["status"] == "skip":
        tags.append("用語::削除候補")          # 只随包发一次,方便一键删卡
    return tags


def main() -> int:
    ap = argparse.ArgumentParser(description="term 表 → Anki 牌组")
    ap.add_argument("--out", default=str(OUT_DIR / "特定技能用語.apkg"))
    ap.add_argument("--json", default=str(OUT_DIR / "terms.json"))
    ap.add_argument("--layout", choices=("tier", "category"), default="tier",
                    help="tier=必須/重要/補完 三个牌组(默认);category=按 13 个类别")
    ap.add_argument("--category", default="", help="只出某个类别")
    ap.add_argument("--min-star", type=int, default=0, dest="min_star")
    ap.add_argument("--status", default="", help="只出某状态的(如 keep)")
    ap.add_argument("--include-common", action="store_true",
                    help="连 common_claim 也出(默认出)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-update-db", action="store_true")
    a = ap.parse_args()

    conn = connect()
    init_db(conn)
    q = "SELECT * FROM term WHERE confidence IN ('established','common_claim')"
    p: list = []
    if a.layout == "tier":
        q += " AND (status<>'skip' OR skip_sent=0)"   # 剔除词只随包发一次
    else:
        q += " AND status<>'skip'"
    if a.category:
        q += " AND category=?"
        p.append(a.category)
    if a.min_star:
        q += " AND difficulty>=?"
        p.append(a.min_star)
    if a.status:
        q += " AND status=?"
        p.append(a.status)
    q += (" ORDER BY (status='skip'), tier, priority DESC, hits DESC, term"
          if a.layout == "tier" else
          " ORDER BY category, difficulty DESC, hits DESC, term")
    rows = [dict(r) for r in conn.execute(q, p)]
    if not rows:
        print("[term_cards] 没有可出的词(检查 confidence / --status 过滤)", file=sys.stderr)
        return 1

    decks: dict[str, genanki.Deck] = {}
    cards = []
    pos: dict[str, int] = {}          # 每个牌组自己数新卡位置,顺序=查询顺序
    for r in rows:
        if a.layout == "tier":
            name = (SKIP_DECK if r["status"] == "skip"
                    else TIER_DECKS.get(r["tier"] or "3", TIER_DECKS["3"]))
        else:
            name = deck_name(r["category"])
        d = decks.get(name) or decks.setdefault(name, genanki.Deck(deck_id(name), name))
        gid = guid(r["term"], r["reading"], r["guid_key"])
        fields = [r["term"], r["reading"], r["zh"] or "", r["note"] or "",
                  r["category"], "★" * r["difficulty"], r["pages"],
                  f'[sound:{r["audio"]}]' if r.get("audio") else ""]
        n = pos.get(name, 0)
        pos[name] = n + 1
        d.add_note(genanki.Note(model=MODEL, fields=fields, guid=gid,
                                tags=tags_for(r), due=n))
        cards.append({"anki_guid": gid, "term": r["term"], "reading": r["reading"],
                      "zh": r["zh"], "note": r["note"], "category": r["category"],
                      "difficulty": r["difficulty"], "pages": r["pages"],
                      "tier": r["tier"], "status": r["status"],
                      "audio": r.get("audio") or "", "deck": name, "tags": tags_for(r)})

    if a.layout == "tier":
        by: dict[str, int] = {}
        for c in cards:
            by[c["deck"]] = by.get(c["deck"], 0) + 1
        print(f"[term_cards] {len(cards)} 张 / {len(decks)} 个牌组(tier 布局)")
        for k, v in sorted(by.items()):
            print(f"    {v:>5}   {k}")
    else:
        by_cat: dict[str, int] = {}
        for c in cards:
            by_cat[c["category"]] = by_cat.get(c["category"], 0) + 1
        print(f"[term_cards] {len(cards)} 张 / {len(decks)} 个牌组"
              f"（uncertain 已排除；共 {len(by_cat)} 类）")
        for k, v in sorted(by_cat.items(), key=lambda x: -x[1]):
            print(f"    {k:<14} {v:>5}   {deck_name(k)}")
    if a.dry_run:
        return 0

    media = [str(MEDIA_DIR / c["audio"]) for c in cards if c.get("audio")
             and (MEDIA_DIR / c["audio"]).exists()]
    if len(media) < sum(1 for c in cards if c.get("audio")):
        print(f"    [warn] 有卡引用了音频但文件不在 {MEDIA_DIR}(先跑 src/term_tts.py)",
              file=sys.stderr)
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    genanki.Package(list(decks.values()), media_files=media).write_to_file(str(out))
    Path(a.json).write_text(json.dumps({"cards": cards}, ensure_ascii=False, indent=1),
                            encoding="utf-8")
    started = now()
    if not a.no_update_db:
        for c in cards:
            if c["status"] == "skip":          # 剔除词:只记「已发过」,不改回 carded
                conn.execute("UPDATE term SET skip_sent=1, updated_at=?"
                             " WHERE term=? AND reading=?",
                             (now(), c["term"], c["reading"]))
            else:
                conn.execute("UPDATE term SET status='carded', updated_at=?"
                             " WHERE term=? AND reading=?",
                             (now(), c["term"], c["reading"]))
        conn.commit()
    log_run(conn, "term_cards", EXPORT_VERSION, started, len(cards), 0,
            f"{out} decks={len(decks)} layout={a.layout}")
    print(f"    音频 {len(media)} 个")
    print(f"    → {out}\n    → {a.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
