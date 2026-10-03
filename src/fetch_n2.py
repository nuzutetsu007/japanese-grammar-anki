#!/usr/bin/env python3
"""抓 JLPT N2 文法清单(japanesetest4you.com)并写成 input/toc/n2_list.json。

站点直接请求会被 Cloudflare 拦(403),所以带上完整的浏览器请求头。
抓完只保留**文法点名称 + 罗马字读音 + 英文释义 + 原页 URL**,
不保存原页正文(项目原则:只存索引和 AI 自己的话)。

用法:
    python3 src/fetch_n2.py            # 抓取并写文件
    python3 src/fetch_n2.py --list     # 只打印
"""
from __future__ import annotations

import argparse
import html
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

ROOT = Path(__file__).resolve().parent.parent
OUT_FILE = ROOT / "input" / "toc" / "n2_list.json"
URL = "https://japanesetest4you.com/jlpt-n2-grammar-list/"

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"
                   " (KHTML, like Gecko) Chrome/131.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-Dest": "document",
}

# 形如 "あるいは (aruiwa)" / "ちっとも～ない (chitto mo~nai)" / "ばかり (bakari) – 2"
READING_RE = re.compile(r"^(?P<title>.*?)\s*[（(](?P<romaji>[A-Za-z0-9'’\- /~]+)[）)]\s*(?P<tail>.*)$")


def normalize(t: str) -> str:
    t = html.unescape(t)
    t = t.replace("～", "〜").replace("~", "〜")
    t = re.sub(r"[\u00a0\s]+", "", t)
    return t.strip()


def split_title(raw: str) -> tuple[str, str, str]:
    """拆出 (日文标题, 罗马字读音, 备注)。"""
    m = READING_RE.match(raw.strip())
    if not m:
        return normalize(raw), "", ""
    title = normalize(m.group("title"))
    romaji = re.sub(r"\s+", " ", m.group("romaji")).strip().replace("~", "〜")
    tail = m.group("tail").strip()
    tail = re.sub(r"^[\-–—\s]+", "", tail).strip()
    return title, romaji, tail


def fetch() -> list[dict]:
    import httpx

    r = httpx.get(URL, headers=HEADERS, timeout=60, follow_redirects=True)
    r.raise_for_status()
    body = r.text

    items: list[dict] = []
    # 条目格式: <p><a href="...">TITLE (romaji)</a>:&nbsp;GLOSS</p>
    # 只用这个模式匹配,能自动排掉导航栏的 Contact / Donate 链接。
    pat = re.compile(
        r'<p>\s*<a\s+href="(https://japanesetest4you\.com/flashcard/[^"]+)"[^>]*>(.*?)</a>'
        r'\s*(?:\((?P<r1>[A-Za-z0-9\'’\-\s/~]+)\))?'
        r'\s*:\s*(?:(?:&nbsp;)|\s)*(?P<gloss>.*?)</p>', re.S)
    for m in pat.finditer(body):
        url = m.group(1)
        txt = html.unescape(re.sub(r"<[^>]+>", "", m.group(2))).strip()
        if m.group("r1"):          # 罗马字读音写在 </a> 后面的那种
            txt = f"{txt} ({m.group('r1')})"
        gloss = html.unescape(re.sub(r"<[^>]+>", "", m.group("gloss")))
        gloss = re.sub(r"\s+", " ", gloss).strip().strip("[]").strip()
        if not txt:
            continue
        title, romaji, tail = split_title(txt)
        items.append({
            "title_ja": title,
            "reading_romaji": romaji,
            "gloss": gloss,
            "page_ref": url,
            "note": tail,
        })
    return items


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    args = ap.parse_args()

    items = fetch()
    if not items:
        print("[fetch_n2] 没抓到条目(url 结构可能变了)", file=sys.stderr)
        return 1

    # 去重(按标题),保留站点顺序
    seen: set[str] = set()
    uniq = []
    for it in items:
        if it["title_ja"] in seen:
            continue
        seen.add(it["title_ja"])
        uniq.append(it)

    data = {
        "source": URL,
        "fetched": "japanesetest4you.com",
        "chapter": "N2文法",
        "items": [
            {**it, "book_order": 1000 + i} for i, it in enumerate(uniq, start=1)
        ],
    }

    if args.list:
        for it in data["items"]:
            print(f"  {it['book_order']:>4}. {it['title_ja']:<28} {it['reading_romaji']:<22}"
                  f" | {it['gloss'][:40]}")
        print(f"\n合计: {len(data['items'])}")
        return 0

    OUT_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[fetch_n2] {len(data['items'])} 条 → {OUT_FILE}")
    print(f"  去重前 {len(items)} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
