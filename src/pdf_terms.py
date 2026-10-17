#!/usr/bin/env python3
"""从 pdf2md 产出的 markdown 里挖「术语候选」(给做 Anki 单词卡用)。

吃 `pdf2md.py --ruby` 的那份(汉字带读音),挖三类:
  A. 片假名外来语(リミットスイッチ / スマッジング …)
  B. 汉字词 + 读音(圧着工具(あっちゃくこうぐ) …)
  C. 短标签行(图里的注记 / 清单项,通常没有句子结构)
  D. 定义句(含「〜とは」「〜といいます」的句子)——做卡背最有用

用法:
    python3 src/pdf_terms.py data/out/pdf_md/studyguide1_02.ruby.md [-o 用語候補.md]
"""

from __future__ import annotations

import argparse
import collections
import re
import sys
from pathlib import Path

KATA = re.compile(r"[\u30a1-\u30f4\u30f6ー]{3,}")
PAIR = re.compile(r"([\u4e00-\u9fff々]{1,6})\(([\u3040-\u309fー]{1,12})\)")
# 合并相邻的 汉字(よみ) 块(圧着(あっちゃく)工具(こうぐ) → 圧着工具)。
# 也允许中间夹**一个非助词假名**: 目盛(めも)り → 目盛り(めもり)、
# 組立(くみた)て区分(くぶん) → 組立て区分。夹助词(の/は/を/に…)就不并,
# 否则正文里每个汉字都带读音,整句会被拼成一个词。
GAP_OK = re.compile(r"[\u3041-\u3096]")      # 平假名一个字
PARTICLE = set("のはがをにでともやへか")
TAIL_NO = set("しす")      # 行尾不吸收的假名(「加工し」这种动词连用形不是词)
MAX_TERM = 8
TAG = re.compile(r"<!--\s*p\.(\d+)\s*-->")
HEAD = re.compile(r"^#{1,6}\s*")
PUNCT = re.compile(r"[。、，．,\.!?！？「」（）()：:；;ー\-–—〜・]")
DEF = re.compile(r"とは|という|といいます|いいます|呼びます|呼び|を指し|のことです|を意味")

# 太常见的片假名,不可能是「生僻术语」,但也不删——单独归一堆
COMMON = {
    "データ", "グラフ", "カラー", "パターン", "サイズ", "レベル", "テスト", "チェック",
    "ポイント", "タイプ", "ケース", "プログラム", "グループ", "メンバー", "ページ",
    "リスト", "キーワード", "テキスト", "センター", "システム", "コンピュータ",
    "マーク", "コース", "コード", "イメージ", "サービス", "エネルギー", "スタート",
    "スイッチ", "モーター", "エネルギー", "ゼロ", "プラス", "マイナス", "メートル",
}


def pages_of(path: Path) -> list[tuple[int, list[str]]]:
    """→ [(页码, [行…])]"""
    out: list[tuple[int, list[str]]] = []
    page, buf = 0, []
    for line in path.read_text(encoding="utf-8").split("\n"):
        m = TAG.search(line)
        if m:
            if buf:
                out.append((page or 1, buf))
            page, buf = int(m.group(1)), []
        elif line.strip():
            buf.append(HEAD.sub("", line).strip())
    if buf:
        out.append((page or 1, buf))
    return out


def merge_pairs(line: str) -> list[tuple[str, str]]:
    """把 汉字(よみ) 块拼成词,并带上送假名。

    规则(踩过坑的版本):
      * 紧邻的块(gap 为空)才拼一起: 圧着(あっちゃく)工具(こうぐ) → 圧着工具
      * 中间夹假名 = **词边界**,而且那个假名归**左边**的词:
          細(ほそ)い一点(いってん)鎖(さ)線(せん) → 細い ✓ | 一点鎖線 ✓
        (早先允许「夹 1 个假名也拼」,结果拼出垃圾词 細い一点鎖 还丢了真词)
      * 结尾紧跟的 1 个假名(非助词、后面不是假名)算送假名:
          目盛(めも)り → 目盛り(めもり)、組立(くみた)て → 組立て
    """
    ms = list(PAIR.finditer(line))
    out: list[tuple[str, str]] = []
    i = 0
    while i < len(ms):
        term, read = ms[i].group(1), ms[i].group(2)
        j = i
        while j + 1 < len(ms):
            gap = line[ms[j].end():ms[j + 1].start()]
            if gap or (j - i) >= 2 or len(term) + len(ms[j + 1].group(1)) > MAX_TERM:
                break
            term += ms[j + 1].group(1)
            read += ms[j + 1].group(2)
            j += 1
        end = ms[j].end()
        if j + 1 < len(ms):                       # 下个块前的假名夹层
            gap = line[end:ms[j + 1].start()]
            if len(gap) == 1 and GAP_OK.match(gap) and gap not in PARTICLE:
                term += gap
                read += gap
        else:                                     # 行尾的送假名
            m = re.match(r"([\u3041-\u3096])", line[end:])
            if m:
                ch = m.group(1)
                nxt = line[end + 1:end + 2]
                if ch not in PARTICLE and ch not in TAIL_NO and not (nxt and GAP_OK.match(nxt)):
                    term += ch
                    read += ch
        out.append((term, read))
        i = j + 1
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="挖术语候选")
    ap.add_argument("md", nargs="+")
    ap.add_argument("-o", "--out", default="")
    ap.add_argument("--min-len", type=int, default=3, help="片假名词最短长度(默认 3)")
    a = ap.parse_args()

    kata: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    kanji: dict[str, dict] = {}
    labels: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    defs: list[tuple[str, int]] = []
    nlines = 0

    for p in a.md:
        f = Path(p)
        if not f.exists():
            print(f"跳过(不存在): {f}", file=sys.stderr)
            continue
        for pno, lines in pages_of(f):
            # 换行处被拆开的片假名先拼回去(プロ\nグラマブル → プログラマブル),
            # 代价是偶尔把相邻两个独立标签也粘在一起——页码列能核对。
            joined = re.sub(r"(?<=[\u30a1-\u30f4\u30f6ー])\n(?=[\u30a1-\u30f4\u30f6ー])", "",
                            "\n".join(lines))
            for ln in lines:
                nlines += 1
                for term, read in merge_pairs(ln):
                    d = kanji.setdefault(term, {"read": collections.Counter(), "pages": collections.Counter()})
                    d["read"][read] += 1
                    d["pages"][pno] += 1
                s = ln.strip()
                if 2 <= len(s) <= 14 and not PUNCT.search(s) and " " not in s:
                    labels[s][pno] += 1
                if DEF.search(ln) and len(ln) >= 12 and not ln.startswith("!"):
                    defs.append((ln, pno))
            for w in KATA.findall(joined):
                if len(w) >= a.min_len:
                    kata[w][pno] += 1

    def pages_str(c: collections.Counter, limit: int = 8) -> str:
        ps = sorted(c)
        s = ", ".join(str(x) for x in ps[:limit])
        return s + ("…" if len(ps) > limit else "")

    L: list[str] = ["# 术语候选", ""]
    L += [f"来源: {', '.join(a.md)}", f"总行数 {nlines}", ""]

    L += [f"## A. 片假名外来语({len(kata)} 个)", "",
          "| 词 | 次数 | 页 |", "|---|---:|---|"]
    tech = sorted((w for w in kata if w not in COMMON),
                  key=lambda w: (sum(kata[w].values()), -len(w)))
    for w in tech:
        L.append(f"| {w} | {sum(kata[w].values())} | {pages_str(kata[w])} |")
    com = sorted(w for w in kata if w in COMMON)
    L += ["", f"<details><summary>常用词({len(com)} 个,基本不用做卡)</summary>", "",
          " " + " / ".join(com), "", "</details>", ""]

    L += [f"## B. 汉字词 + 读音({len(kanji)} 个)", "",
          "| 词 | 读音 | 次数 | 页 |", "|---|---|---:|---|"]
    for term in sorted(kanji, key=lambda t: (sum(kanji[t]["pages"].values()), -len(t))):
        d = kanji[term]
        read = d["read"].most_common(1)[0][0]
        L.append(f"| {term} | {read} | {sum(d['pages'].values())} | {pages_str(d['pages'])} |")

    L += ["", f"## C. 短标签行({len(labels)} 个: 图里的注记/清单项,自己看一眼再决定)", ""]
    for s in sorted(labels, key=lambda s: (sum(labels[s].values()), -len(s))):
        L.append(f"- {s}  <!-- {pages_str(labels[s], 4)} -->")

    L += ["", f"## D. 定义句({len(defs)} 句: 卡背素材)", ""]
    seen = set()
    for s, pno in defs:
        k = s[:24]
        if k in seen:
            continue
        seen.add(k)
        L.append(f"- ({pno}) {s[:160]}")

    text = "\n".join(L) + "\n"
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(text, encoding="utf-8")
        print(f"→ {a.out}")
    else:
        print(text)
    print(f"片假名 {len(kata)} / 汉字词 {len(kanji)} / 标签 {len(labels)} / 定义句 {len(defs)}",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
