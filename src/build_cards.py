#!/usr/bin/env python3
"""阶段 6:生成卡片(build_cards)。

只用通过审核的内容(status='verified'/'done';可加 --include-expanded)。
生成 3 种卡(名字要和 export_anki.MODELS 对得上):
  1. reading       読解卡:正面第1条例句(文法点高亮);背面意思/訳/文型/接続/用法/使い分け/例文。
  2. point         文型卡:正面文法点;背面意思/読み/接続/用法/使い分け/例文。
  3. etymology_hook 联想卡:正面文法点;背面漢字表記/由来(标定/通説/助记)/意味。

输出:
  - card 表(anki_guid 稳定,重跑覆盖而不重复)
  - data/out/cards.json(给 export_anki.py 用)

用法:
    python src/build_cards.py
    python src/build_cards.py --include-expanded
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import cards_path_for, connect, init_db, log_run, now  # noqa: E402

CARD_SOURCE = "build_cards_v1"


CARD_CONF = {"established", "common_claim"}


def guid(grammar_id: int, card_type: str) -> str:
    return hashlib.sha1(f"grammar-anki:N1:{grammar_id}:{card_type}".encode()).hexdigest()[:16]


def variants(title: str) -> list[str]:
    out = []
    for v in re.split(r"[／/]", title):
        v = v.replace("〜", "").replace("～", "").strip()
        if not v:
            continue
        out.append(v)
        nop = re.sub(r"[（(][^）)]*[）)]", "", v).strip()
        if nop and nop != v:
            out.append(nop)
    return sorted(set(out), key=len, reverse=True)


def find_match(ja: str, title: str):
    """在例句里找出该文法点的那一段。返回 (start, end) 或 None。"""
    for v in variants(title):
        if len(v) >= 2 and v in ja:
            i = ja.index(v)
            return i, i + len(v)
    # 退化:去掉尾部送假名/助词再试
    for v in variants(title):
        for cut in range(len(v) - 1, 1, -1):
            sub = v[:cut]
            if len(sub) >= 2 and sub in ja:
                i = ja.index(sub)
                return i, i + len(sub)
    return None


def highlight(ja: str, title: str) -> str:
    """把例句里的文法点用 <span class='hl'> 标出来(读解时提醒看哪里)。"""
    m = find_match(ja, title)
    if not m:
        return html.escape(ja)
    i, j = m
    return (html.escape(ja[:i])
            + f'<span class="hl">{html.escape(ja[i:j])}</span>'
            + html.escape(ja[j:]))


def pick_fields(conn, gid: int) -> dict:
    fields: dict[str, tuple[str, str]] = {}
    for e in conn.execute(
        "SELECT * FROM expansion WHERE grammar_id=? AND prompt_version NOT LIKE '%:superseded'"
        " ORDER BY id",
        (gid,),
    ):
        # 后写的覆盖先写的(重跑时新内容在后)
        fields[e["field"]] = (e["content"], e["confidence"])
    return fields


def pick_examples(conn, gid: int) -> list:
    return conn.execute(
        "SELECT * FROM example WHERE grammar_id=? AND prompt_version NOT LIKE '%:superseded'"
        " ORDER BY id",
        (gid,),
    ).fetchall()


def audio_tag(e, i: int) -> str:
    """第 1 句用 [sound:](Anki 在答案面会自动播);第 2 句用 <audio>+按钮,
    否则 [sound:] 会跟第 1 句**依次连播**(Anki 对多个 sound 就是这么干的)。
    注音颗只在 example.audio 有值(即 tts.py 跑过)时出现。"""
    name = (e["audio"] or "") if "audio" in e.keys() else ""
    if not name:
        return ""
    if i == 1:
        return f"[sound:{name}] "
    if i == 2:
        aid = f"au{e['id']}"
        return (f'<audio id="{aid}" src="{name}" preload="none"></audio>'
                f'<a class="audbtn" href="#" title="再听一遍"'
                f" onclick=\"document.getElementById('{aid}').play();return false;\">\u25b6\u2009再听</a> ")
    return ""


def render_examples(examples: list) -> str:
    """把例句渲染成一块 HTML:每条带「接续」标签,方便一次看到不同接续的用法。"""
    blocks = []
    for i, e in enumerate(examples, 1):
        conn_tag = f'<span class="conn">{html.escape(e["conn"])}</span>' if e["conn"] else ""
        blocks.append(
            f'<div class="ex">'
            f'<div class="exh"><span class="num">{i}</span>{conn_tag}{audio_tag(e, i)}</div>'
            f'<div class="ja">{html.escape(e["ja"])}</div>'
            f'<div class="rd">{html.escape(e["reading"] or "")}</div>'
            f'<div class="zh">{html.escape(e["zh"] or "")}</div>'
            f'</div>'
        )
    return "".join(blocks)


def build(conn, include_expanded: bool, chapter: str = "") -> list[dict]:
    statuses = ["verified", "done"] + (["expanded"] if include_expanded else [])
    sql = "SELECT * FROM grammar_point WHERE status IN ({})".format(
        ",".join("?" * len(statuses)))
    params: list = list(statuses)
    if chapter:
        sql += " AND chapter LIKE ?"
        params.append(f"%{chapter}%")
    sql += " ORDER BY book_order"
    rows = conn.execute(sql, params).fetchall()

    cards: list[dict] = []
    for r in rows:
        gid = r["id"]
        fields = pick_fields(conn, gid)
        examples = pick_examples(conn, gid)

        def get(name: str) -> str:
            entry = fields.get(name)
            if not entry or entry[1] not in CARD_CONF:
                return ""
            return entry[0]

        conn_text = get("connection")
        meaning = get("meaning")
        usage = get("usage_note")
        contrast = get("contrast")
        kanji = get("kanji_writing")
        etym = fields.get("etymology")
        etym_text, etym_conf = (etym if etym else ("", ""))
        if etym_conf == "uncertain":
            etym_text = ""

        title = r["title_ja"]
        reading = r["reading"] or title
        ex0 = examples[0] if examples else None
        ex_html = render_examples(examples)

        # ---- 1) 読解卡:日语例句 → 中文意思(主卡)----
        #    N1/N2 文型主要是读解用的,练习方向是「看到日语 → 想起中文意思」
        if ex0 and meaning:
            sent_html = highlight(ex0["ja"], title)
            front = (
                "<div class='ask'>文の意味をつかむ</div>"
                f"<div class='sent'>{sent_html}</div>"
            )
            back = (
                "<hr id='answer'>"
                f"<div class='ans'>{html.escape(meaning)}</div>"
                f"<div class='lbl'>訳</div><div class='ex'>{html.escape(ex0['ja'])}"
                f"<div class='rd'>{html.escape(ex0['reading'] or '')}</div>"
                f"<div class='zh'>{html.escape(ex0['zh'] or '')}</div></div>"
                f"<div class='lbl'>文型</div><div>{html.escape(title)}"
                f"<span class='rd'>{html.escape(reading)}</span></div>"
                f"<div class='lbl'>接続</div><div>{html.escape(conn_text)}</div>"
                + (f"<div class='lbl'>用法</div><div>{html.escape(usage)}</div>" if usage else "")
                + (f"<div class='lbl'>使い分け</div><div>{html.escape(contrast)}</div>" if contrast else "")
                + (f"<div class='lbl'>例文(接続別)</div>{ex_html}" if ex_html else "")
            )
            cards.append({
                "grammar_id": gid, "card_type": "reading", "anki_guid": guid(gid, "reading"),
                "chapter": r["chapter"],
                "front": front, "back": back,
                "fields": {
                    "Sentence": sent_html,
                    "SentenceReading": ex0["reading"] or "",
                    "SentenceZh": ex0["zh"] or "",
                    "Meaning": meaning,
                    "Grammar": title, "Reading": reading,
                    "Connection": conn_text, "Usage": usage, "Contrast": contrast,
                    "Examples": ex_html,
                },
            })

        # ---- 2) 文型卡:文法点 → 中文意思 ----
        if meaning:
            front = ("<div class='ask'>この文型の意味は？</div>"
                     f"<div class='sent'>{html.escape(title)}</div>")
            back = (
                "<hr id='answer'>"
                f"<div class='ans'>{html.escape(meaning)}</div>"
                f"<div class='lbl'>読み</div><div class='rd'>{html.escape(reading)}</div>"
                f"<div class='lbl'>接続</div><div>{html.escape(conn_text)}</div>"
                + (f"<div class='lbl'>用法</div><div>{html.escape(usage)}</div>" if usage else "")
                + (f"<div class='lbl'>使い分け</div><div>{html.escape(contrast)}</div>" if contrast else "")
                + (f"<div class='lbl'>例文(接続別)</div>{ex_html}" if ex_html else "")
            )
            cards.append({
                "grammar_id": gid, "card_type": "point", "anki_guid": guid(gid, "point"),
                "chapter": r["chapter"],
                "front": front, "back": back,
                "fields": {
                    "Grammar": title, "Reading": reading, "Meaning": meaning,
                    "Connection": conn_text, "Usage": usage, "Contrast": contrast,
                    "Examples": ex_html,
                },
            })

        # ---- 3) 联想卡(可选):文法点 → 汉字写法/由来 ----
        label = ""
        if etym_text and etym_conf == "established":
            label = "由来(定説)"
        elif etym_text and etym_conf == "common_claim":
            label = "由来(通説・諸説あり)"
        elif etym_text and etym_conf == "mnemonic_only":
            label = "联想记忆(非历史事实)"
        if kanji or etym_text:
            front = (f"<div class='ask'>漢字で書くと？ 由来は？</div>"
                     f"<div class='sent'>{html.escape(title)}</div>")
            back = (
                f"<div class='ans'>{html.escape(title)}</div>"
                + (f"<div class='lbl'>漢字表記</div><div>{html.escape(kanji)}</div>" if kanji else "")
                + (f"<div class='lbl'>{html.escape(label)}</div><div>{html.escape(etym_text)}</div>"
                   if etym_text else "")
                + (f"<div class='lbl'>意味</div><div>{html.escape(meaning)}</div>" if meaning else "")
            )
            cards.append({
                "grammar_id": gid, "card_type": "etymology_hook",
                "anki_guid": guid(gid, "etymology_hook"),
                "chapter": r["chapter"],
                "front": front, "back": back,
                "fields": {
                    "Grammar": title, "Reading": reading, "KanjiWriting": kanji,
                    "EtymologyLabel": label, "Etymology": etym_text, "Meaning": meaning,
                    "Connection": conn_text,
                },
            })

    # 卡片型变了、或某个文法点退出 verified 时,把不再属于本次生成集合的旧卡清掉,
    # 保证 card 表 == 可导出的牌组。
    # 注意:按 anki_guid 精确对照,而不是只看 card_type —— 否则 status 变成
    # needs_review 的文法点会把旧卡遗留在 card 表里。
    keep = {c["anki_guid"] for c in cards}
    removed = 0
    if not chapter:
        for old in conn.execute("SELECT id, anki_guid FROM card").fetchall():
            if old["anki_guid"] not in keep:
                conn.execute("DELETE FROM card WHERE id=?", (old["id"],))
                removed += 1
    conn.commit()
    if removed:
        print(f"  [cleanup] 清理 {removed} 张已失效的旧卡")
    # 写回 card 表(anki_guid 唯一 → 更新,不新增)
    for c in cards:
        old = conn.execute("SELECT id, version FROM card WHERE anki_guid=?", (c["anki_guid"],)).fetchone()
        if old:
            conn.execute(
                "UPDATE card SET front=?, back=?, status='draft', version=version+1 WHERE id=?",
                (c["front"], c["back"], old["id"]),
            )
            c["version"] = old["version"] + 1
        else:
            conn.execute(
                "INSERT INTO card (grammar_id, card_type, front, back, anki_guid, version, status)"
                " VALUES (?,?,?,?,?,1,'draft')",
                (c["grammar_id"], c["card_type"], c["front"], c["back"], c["anki_guid"]),
            )
            c["version"] = 1
    conn.commit()
    return cards


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--include-expanded", action="store_true",
                    help="把还没自检的 expanded 条目也生成卡片")
    ap.add_argument("--chapter", default="", help="只生成某个章节的卡(如 N2文法)")
    ap.add_argument("--out", default="", help="输出路径(默认 data/out/cards.json)")
    args = ap.parse_args()

    started = now()
    conn = connect()
    init_db(conn)
    cards = build(conn, args.include_expanded, args.chapter)

    out = Path(args.out) if args.out else cards_path_for(args.chapter)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"cards": cards}, ensure_ascii=False, indent=2), encoding="utf-8")

    by_type: dict[str, int] = {}
    for c in cards:
        by_type[c["card_type"]] = by_type.get(c["card_type"], 0) + 1
    log_run(conn, "build_cards", CARD_SOURCE, started, len(cards), 0, json.dumps(by_type, ensure_ascii=False))
    print(f"[build_cards] {len(cards)} 张卡 → {out}")
    for k, v in sorted(by_type.items()):
        print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
