#!/usr/bin/env python3
"""阶段 7:导出 Anki(export_anki)。

读 data/out/cards.json(由 build_cards.py 生成),用 genanki 打包 .apkg。
`anki_guid` 由 grammar_id + card_type 稳定生成 → 数据库修正后再导入,
Anki 里是**更新**而不是新增重复卡。

用法:
    python src/export_anki.py
    python src/export_anki.py --out data/out/日本語文法.apkg
    python src/export_anki.py --db-only      # 只导出已经在 card 表里的 exported/draft
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import genanki  # noqa: E402

from common import (  # noqa: E402
    MEDIA_DIR, OUT_DIR, cards_path_for, connect, init_db, log_run, now)

# chapter → 牌组。deck_id 固定(改了 Anki 会当成新牌组,老卡要手删)。
DECK_MAP = [
    (re.compile(r"^第[1-6]週"), 2059400111, "日本語文法::N1"),
    (re.compile(r"^N2"),        2059400112, "日本語文法::N2"),
]
DECK_FALLBACK = (2059400199, "日本語文法::その他")
TAG_ROOT = "文法"                       # 标签树:文法 / 文法::N1 / 文法::N1::第1週1日目
EXPORT_VERSION = "export_anki_v1"

CSS = """
/*
  样式说明(按官方文档 + AnkiDroid wiki 的 night mode 实践):
  - Anki Desktop / AnkiMobile 的 class 是 `.nightMode`(驼峰)
  - AnkiDroid 的 class 是 `.night_mode`(下划线)
  - `prefers-color-scheme: dark` 在 AnkiDroid / AnkiMobile 有效,
    但 **Anki Desktop 目前不报 dark**(Qt webview 的已知问题),所以不能只靠它
  因此:变量定义在 .card 上,三种选择器各覆盖一次。
*/

:root { color-scheme: light dark; }

.card {
  /* 亮色(默认) */
  --bg:      #fbfbfd;
  --fg:      #1c1c1e;
  --muted:   #64646b;   /* 次级文字,对 --bg 对比度 ~7:1(WCAG AA+) */
  --faint:   #8e8e96;
  --line:    #dcdce2;
  --panel:   #f3f3f7;
  --accent:  #0b57d0;   /* 答案 */
  --label:   #96400a;   /* 小标题 */
  --zh:      #1f6f1f;   /* 中文翻译 */
  --chip:    #e7effc;   /* 接续标签底色 */

  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
               "Noto Sans JP", "Hiragino Kaku Gothic ProN", "Yu Gothic", Meiryo, sans-serif;
  font-size: 19px;
  line-height: 1.7;
  text-align: left;
  background-color: var(--bg);
  color: var(--fg);
}

/* 暗色变量块(下边两处共用) */
.card.nightMode, .nightMode .card,      /* Anki Desktop + AnkiMobile */
.card.night_mode, .night_mode .card {   /* AnkiDroid */
  --bg:      #1e1e22;
  --fg:      #e8e8ec;
  --muted:   #a8a8b0;
  --faint:   #7e7e88;
  --line:    #3a3a42;
  --panel:   #26262d;
  --accent:  #8ab4f8;
  --label:   #f0b060;
  --zh:      #8fce90;
  --chip:    #2c3a4e;
}

/* 跟随系统的客户端 */
@media (prefers-color-scheme: dark) {
  .card {
    --bg:      #1e1e22;
    --fg:      #e8e8ec;
    --muted:   #a8a8b0;
    --faint:   #7e7e88;
    --line:    #3a3a42;
    --panel:   #26262d;
    --accent:  #8ab4f8;
    --label:   #f0b060;
    --zh:      #8fce90;
    --chip:    #2c3a4e;
  }
}

.ask  { color: var(--muted); font-size: 15px; margin-bottom: .55em; letter-spacing: .03em; }
.sent { font-size: 22px; line-height: 1.75; margin-bottom: .5em; }
.hint { color: var(--faint); font-size: 14px; }
.hl   { color: var(--accent); font-weight: 700; }
.ans  { font-size: 26px; font-weight: 700; color: var(--accent); line-height: 1.4; }
.ans .rd { font-size: 15px; font-weight: 400; color: var(--muted); margin-left: .6em; }
.lbl  { color: var(--label); font-size: 13px; font-weight: 700; letter-spacing: .08em;
        margin: 1em 0 .15em; padding-left: .5em; border-left: 3px solid var(--label); }
.rd   { color: var(--muted); font-size: 15px; line-height: 1.5; }
.zh   { color: var(--zh); font-size: 16px; margin-top: .1em; }
.ex   { background: var(--panel); border-radius: 8px; padding: .6em .8em; margin: .45em 0; }
.ex .ja  { font-size: 19px; line-height: 1.65; }
.ex .exh { display: flex; align-items: center; gap: .5em; margin-bottom: .3em;
           font-size: 12px; letter-spacing: .04em; }
.ex .num { display: inline-block; min-width: 1.4em; height: 1.4em; line-height: 1.4em;
           text-align: center; border-radius: 50%; background: var(--line);
           color: var(--muted); font-weight: 700; }
.conn { display: inline-block; padding: .1em .5em; border-radius: 999px;
        background: var(--chip); color: var(--accent); font-weight: 700; font-size: 12px; }
/* 第 2 句的手动播放按钮(第 1 句用 Anki 原生的 [sound:] 按钮,自动播放) */
.audbtn { display: inline-flex; align-items: center; gap: .25em; text-decoration: none;
          padding: .05em .5em; border-radius: 999px; font-size: 12px; font-weight: 700;
          color: var(--accent); background: var(--chip); border: 1px solid var(--line); }
.audbtn:hover { filter: brightness(1.08); }
.ex > audio { display: none; }
hr#answer { border: none; border-top: 1px solid var(--line); margin: 1em 0 .8em; }
"""


# note type 的**身份是 id**,不是名字。2026-09-29 那一版用的是下面这组 id
# (名字带 N1 前缀)。沿用同一批 id,导入时 Anki 才会把它当成**同一个** note type
# 原地更新(名字被改成新的),而不是新建一套「文法-読解」并把旧的那套留成僵尸。
MODEL_IDS = {
    "文法-読解": 660591022,
    "文法-文型": 2412699645,
    "文法-联想": 45774564,
}


def _mid(name: str) -> int:
    """确定性 model id(python 的 hash() 每个进程都不同,不能用)。"""
    if name in MODEL_IDS:
        return MODEL_IDS[name]
    return int(hashlib.sha1(f"grammar-anki:model:{name}".encode()).hexdigest()[:8], 16)


def model(name: str, fields: list[str], front: str, back: str) -> tuple[genanki.Model, list[str]]:
    m = genanki.Model(
        _mid(name),
        name,
        fields=[{"name": f} for f in fields],
        templates=[{"name": name, "qfmt": front, "afmt": back}],
        css=CSS,
    )
    return m, fields


M_READING = model(
    "文法-読解",
    ["Sentence", "SentenceReading", "SentenceZh", "Meaning", "Grammar", "Reading",
     "Connection", "Usage", "Contrast", "Examples"],
    '<div class="ask">文の意味をつかむ</div>\n<div class="sent">{{Sentence}}</div>',
    '<hr id="answer">\n'
    '<div class="ans">{{Meaning}}</div>\n'
    '<div class="lbl">訳</div><div class="ex">{{Sentence}}'
    '<div class="rd">{{SentenceReading}}</div>'
    '<div class="zh">{{SentenceZh}}</div></div>\n'
    '<div class="lbl">文型</div><div>{{Grammar}}<span class="rd">{{Reading}}</span></div>\n'
    '{{#Connection}}<div class="lbl">接続</div><div>{{Connection}}</div>{{/Connection}}\n'
    '{{#Usage}}<div class="lbl">用法</div><div>{{Usage}}</div>{{/Usage}}\n'
    '{{#Contrast}}<div class="lbl">使い分け</div><div>{{Contrast}}</div>{{/Contrast}}\n'
    '{{#Examples}}<div class="lbl">例文(接続別)</div>{{Examples}}{{/Examples}}',
)

M_POINT = model(
    "文法-文型",
    ["Grammar", "Reading", "Meaning", "Connection", "Usage", "Contrast", "Examples"],
    '<div class="ask">この文型の意味は？</div>\n<div class="sent">{{Grammar}}</div>',
    '<hr id="answer">\n'
    '<div class="ans">{{Meaning}}</div>\n'
    '<div class="lbl">読み</div><div class="rd">{{Reading}}</div>\n'
    '{{#Connection}}<div class="lbl">接続</div><div>{{Connection}}</div>{{/Connection}}\n'
    '{{#Usage}}<div class="lbl">用法</div><div>{{Usage}}</div>{{/Usage}}\n'
    '{{#Contrast}}<div class="lbl">使い分け</div><div>{{Contrast}}</div>{{/Contrast}}\n'
    '{{#Examples}}<div class="lbl">例文(接続別)</div>{{Examples}}{{/Examples}}',
)


M_ETYMOLOGY = model(
    "文法-联想",
    ["Grammar", "Reading", "KanjiWriting", "EtymologyLabel", "Etymology", "Meaning", "Connection"],
    '<div class="ask">漢字で書くと？ 由来は？</div>\n<div class="sent">{{Grammar}}</div>',
    '<div class="ans">{{Grammar}}<span class="rd">{{Reading}}</span></div>\n'
    '{{#KanjiWriting}}<div class="lbl">漢字表記</div><div>{{KanjiWriting}}</div>{{/KanjiWriting}}\n'
    '{{#Etymology}}<div class="lbl">{{EtymologyLabel}}</div><div>{{Etymology}}</div>{{/Etymology}}\n'
    '{{#Meaning}}<div class="lbl">意味</div><div>{{Meaning}}</div>{{/Meaning}}',
)

MODELS = {
    "reading": M_READING,
    "point": M_POINT,
    "etymology_hook": M_ETYMOLOGY,
}


def tags_for(chapter: str) -> list[str]:
    """给 note 打标签,Anki 侧边栏可以按 文法 / 文法::N1 / 文法::N1::第1週1日目 过滤。"""
    ch = (chapter or "").strip()
    if ch.startswith("第"):
        level = "N1"
    elif ch.startswith("N2"):
        level = "N2"
    else:
        return [TAG_ROOT] if ch else []
    tags = [TAG_ROOT, f"{TAG_ROOT}::{level}"]
    leaf = re.sub(r"\s+", "", ch)
    if leaf.startswith("第"):
        tags.append(f"{TAG_ROOT}::{level}::{leaf}")
    return tags


def deck_for(chapter: str) -> tuple[int, str]:
    """把 chapter 映射到 (deck_id, 牌组名)。deck_id 固定,别改。"""
    for pat, did, name in DECK_MAP:
        if pat.search(chapter or ""):
            return did, name
    return DECK_FALLBACK


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(OUT_DIR / "日本語文法.apkg"))
    ap.add_argument("--cards", default="",
                    help="cards.json 路径(缺省:全量读 data/out/cards.json,"
                         "指定 --chapter 时读对应的 cards_<chapter>.json)")
    ap.add_argument("--chapter", default="", help="只导出某个章节(如 N2文法)")
    ap.add_argument("--no-update-db", action="store_true")
    args = ap.parse_args()

    cards_path = Path(args.cards) if args.cards else cards_path_for(args.chapter)
    if not cards_path.exists():
        print(f"找不到 {cards_path},请先运行 python3 src/build_cards.py", file=sys.stderr)
        return 1
    cards = json.loads(cards_path.read_text(encoding="utf-8"))["cards"]
    if args.chapter:
        cards = [c for c in cards if args.chapter in (c.get("chapter") or "")]

    decks: dict[int, genanki.Deck] = {}
    counts: dict[str, int] = {}
    exported: list[str] = []
    for c in cards:
        m, field_names = MODELS.get(c["card_type"], (None, None))
        if not m:
            continue
        did, dname = deck_for(c.get("chapter", ""))
        deck = decks.get(did)
        if deck is None:
            deck = decks[did] = genanki.Deck(did, dname)
        f = c["fields"]
        fields = [str(f.get(name, "") or "") for name in field_names]
        deck.add_note(genanki.Note(model=m, fields=fields, guid=c["anki_guid"],
                                   tags=tags_for(c.get("chapter", ""))))
        counts[dname] = counts.get(dname, 0) + 1
        exported.append(c["anki_guid"])

    if not decks:
        print("[export_anki] 没有可导出的卡", file=sys.stderr)
        return 1

    # 卡片 HTML 里引用的音频:第 1 句是 [sound:x.mp3],第 2 句是 <audio src="x.mp3">。
    # 两种都要收集进 media_files,否则 Anki 里播不出来。
    wanted: set[str] = set()
    for c in cards:
        hay = c["front"] + c["back"] + " ".join(str(v) for v in c["fields"].values())
        wanted |= set(re.findall(r"\[sound:([^\]]+)\]", hay))
        wanted |= set(re.findall(r'src="([^"]+\.mp3)"', hay))
    media_files = []
    for name in sorted(wanted):
        p = MEDIA_DIR / name
        if p.exists():
            media_files.append(str(p))
        else:
            print(f"  [warn] 卡片引用了 {name},但 {p} 不存在(先跑 python3 src/tts.py)",
                  file=sys.stderr)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    genanki.Package(list(decks.values()), media_files=media_files).write_to_file(str(out_path))

    started = now()
    conn = connect()
    init_db(conn)
    if not args.no_update_db:
        for g in exported:
            conn.execute("UPDATE card SET status='exported' WHERE anki_guid=?", (g,))
        conn.commit()
    note = (f"{out_path} " + " ".join(f"{k}={v}" for k, v in counts.items())
            + f" media={len(media_files)}")
    log_run(conn, "export_anki", EXPORT_VERSION, started, len(exported), len(cards) - len(exported), note)
    print(f"[export_anki] {len(exported)} 张卡 / {len(decks)} 个牌组 / "
          f"{len(media_files)} 个音频 → {out_path}")
    for dname, n in sorted(counts.items()):
        print(f"    {dname}: {n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
