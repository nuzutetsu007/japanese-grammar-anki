#!/usr/bin/env python3
"""PDF → Markdown(专治日文教材的振り仮名)。

为什么不直接用 markitdown / pdftotext:
  它们按 PDF 内部文字流顺序输出,振り仮名和正文是**两行**交错:
      せいぞうぶん や とくてい ぎ の う
      製造分野特定技能１号 評 価試験
  读的时候要自己拼,198 页的教材基本没法读。

这里改用 poppler 的 `pdftohtml -xml`(每个文本块带字号 + top/left 坐标):
  1. **几何配对**认出振り仮名:一块纯假名 + 它的正下方有同 x、字更大的基块
     (比「字号 < 正文×0.75」稳 —— 标题页是 39/19pt,正文里是 16/8pt,
      全局阈值会漏掉前者)
  2. 按坐标重排成行、按行距切段落,大字号当标题
  3. --ruby 时把假名按字符挂回原位: 製(せい)造(ぞう)…

依赖: poppler(pdftohtml)。用法:
    python3 src/pdf2md.py in.pdf [-o out.md] [--images] [--ruby]

  --images  抽插图到 <名>_media/ 并在正文里引用(默认不抽,快)
  --ruby    额外输出保留振り仮名的一份 <名>.ruby.md
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

HEAD1_RATIO = 1.55     # >= 正文×此值 → ##
HEAD2_RATIO = 1.3      # >= 正文×此值 → ###(这书正文混用 18/22pt,阈值低了整段正文都变标题)
LINE_TOL = 0.45        # top 差在正文行高×此值内算同一行
PARA_GAP = 1.5         # 行距 > 行高×此值 → 另起一段
# 要不要补空格: 日文没有词间空格,而逐字排版的书里字距会被当成间隙。
# 所以按两侧字符的种类分开定阈值(单位: 字号)。
GAP_CJK = 0.45         # 汉字/假名之间: 只有明显的分栏间隙才断开
GAP_LATIN = 0.22       # 拉丁字母/数字之间: 词间空格本来就这么宽
GAP_MIX = 0.28

CJK_RE = re.compile(r"[\u3000-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff00-\uffef々ー]")
# poppler 会把逐字排版的书拆成「圧 着」这种带空格的文本节点。
# 日文没有词间空格 → 夹在两个汉字/假名之间的空白一律删掉;西文两侧的保留。
CJK_SPACE = re.compile(r"(?<=[\u3000-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff00-\uffef々ー])"
                       r"[ \t]+"
                       r"(?=[\u3000-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff00-\uffef々ー])")


def needs_space(prev: "Item", cur: "Item", size: float) -> bool:
    gap = cur.left - prev.right
    if gap <= 0:
        return False
    a = prev.text[-1:] or " "
    b = cur.text[:1] or " "
    if CJK_RE.match(a) and CJK_RE.match(b):
        return gap > size * GAP_CJK
    if a.isascii() and b.isascii():
        return gap > size * GAP_LATIN
    return gap > size * GAP_MIX
RUBY_DY = 1.4          # 假名与基块的最大垂直距离(×较大字号的)
RUBY_SIZE = 1.15       # 基块至少比假名大这么多倍

KANA_RE = re.compile(r"^[\u3040-\u309f\u30a0-\u30ffー]+$")


def run_pdftohtml(pdf: Path, img_dir: Path | None) -> str:
    if not shutil.which("pdftohtml"):
        sys.exit("找不到 pdftohtml,先装 poppler")
    cmd = ["pdftohtml", "-xml", "-stdout"]
    if img_dir is None:
        cmd.append("-i")           # 不抽图
    else:
        img_dir.mkdir(parents=True, exist_ok=True)
    p = subprocess.run(cmd + [str(pdf.resolve())],
                       cwd=str(img_dir) if img_dir else None,
                       capture_output=True, text=True)
    if p.returncode != 0:
        sys.exit(f"pdftohtml 失败: {p.stderr[:400]}")
    return p.stdout


class Item:
    def __init__(self, top: float, left: float, width: float, size: float,
                 text: str, img: str = ""):
        self.top, self.left, self.width, self.size = top, left, width, size
        self.text, self.img = text, img
        self.is_ruby = False
        self.rubies: list[tuple[float, str]] = []   # 挂在**基块**上: (假名中心 x, 假名)

    @property
    def right(self) -> float:
        return self.left + self.width

    def __repr__(self):
        return f"<{'RUBY' if self.is_ruby else 'item'} {self.size:g}@{self.top:.0f},{self.left:.0f} {self.text[:12]!r}>"


def parse(xml: str) -> list[tuple[int, list[Item]]]:
    """→ [(页码, [Item...])]"""
    # pdftohtml 偶发未转义的 &,给个兜底
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        xml = re.sub(r"&(?!(?:amp|lt|gt|quot|apos|#\d+|#x[0-9a-fA-F]+);)", "&amp;", xml)
        root = ET.fromstring(xml)

    pages: list[tuple[int, list[Item]]] = []
    for pg in root.iter("page"):
        sizes: dict[str, float] = {}
        for fs in pg.iter("fontspec"):
            sizes[fs.get("id", "")] = float(fs.get("size", 0))
        items: list[Item] = []
        for el in pg:
            top = float(el.get("top", 0) or 0)
            left = float(el.get("left", 0) or 0)
            if el.tag == "text":
                size = sizes.get(el.get("font", ""), float(el.get("height", 0) or 0))
                txt = "".join(el.itertext())
                txt = CJK_SPACE.sub("", txt)
                if txt.strip():
                    items.append(Item(top, left, float(el.get("width", 0) or 0), size, txt))
            elif el.tag == "image":
                items.append(Item(top, left, 0, 0, "", el.get("src", "")))
        if items:
            pages.append((int(pg.get("number", len(pages) + 1)), items))
    return pages


def body_size(pages: list[tuple[int, list[Item]]]) -> float:
    """按**字符数**加权取众数字号 = 正文字号(页眉/标题会输给正文)。"""
    hist: Counter[float] = Counter()
    for _, items in pages:
        for it in items:
            if not it.img:
                hist[round(it.size, 1)] += len(it.text.strip())
    return hist.most_common(1)[0][0] if hist else 16.0


def page_body(items: list[Item], fallback: float) -> float:
    """单页自己的正文字号 —— 这本书有的页正文是 21pt、有的是 18pt,
    用全局字号去判标题会把整页正文判成标题。"""
    hist: Counter[float] = Counter()
    for i in items:
        if not i.img and i.size > 0:
            hist[round(i.size, 1)] += len(i.text.strip())
    return hist.most_common(1)[0][0] if hist else fallback


def detect_ruby(items: list[Item], body: float) -> int:
    """就地标记振り仮名,并把假名挂到基块上。返回认出多少块。幂等。

    判据: 纯假名块 R + 它的**正下方**有字更大、水平区间重叠的基块 B。
    用区间重叠而不是 left 对齐 —— 一个宽基块(如「（学科試験・実技試験）」)
    头上可以散着好几个假名块,left 差很远但区间是重叠的。
    """
    for i in items:
        i.is_ruby, i.rubies = False, []
    nb = [i for i in items if not i.img]
    n = 0
    for r in nb:
        kana = re.sub(r"\s+", "", r.text)      # pdftohtml 会在字间插空格: 'が っ か'
        if r.size >= body * 1.02:            # 和正文一样大的假名行不是注音
            continue
        if not KANA_RE.match(kana):
            continue
        best, bestd = None, None
        for b in nb:
            if b is r or b.size < r.size * RUBY_SIZE:
                continue
            if KANA_RE.match(re.sub(r"\s+", "", b.text)):
                continue
            scale = max(b.size, r.size)
            dt = b.top - r.top
            if not (0 <= dt <= scale * RUBY_DY):
                continue
            ov = min(r.right, b.right) - max(r.left, b.left)
            if ov < min(r.width, b.width) * 0.35:   # 至少要真重叠一部分
                continue
            if bestd is None or (dt, -ov) < bestd:
                best, bestd = b, (dt, -ov)
        if best is not None:
            r.is_ruby = True
            best.rubies.append((r.right, kana))   # 用右边界: 整词假名才会挂到词尾
            n += 1
    return n


def with_ruby(it: Item) -> str:
    """把基块头上的假名按 x 位置插回对应字符后面: 学科(がっか)試験(しけん)。"""
    rs = sorted(it.rubies)
    if not rs:
        return it.text
    n = len(it.text)
    if n < 2 or it.width <= 0:
        return it.text + "".join(f"({t})" for _, t in rs)
    parts: list[str] = []
    ri = 0
    for ci, ch in enumerate(it.text):
        parts.append(ch)
        edge = it.left + it.width * (ci + 1) / n     # 该字右边界
        while ri < len(rs) and rs[ri][0] <= edge:
            parts.append(f"({rs[ri][1]})")
            ri += 1
    parts += [f"({t})" for _, t in rs[ri:]]
    return "".join(parts)


def to_lines(items: list[Item], body: float, inline_ruby: bool):
    """→ [(top, 文本, 是否图, 字号)]"""
    lines: list[list[Item]] = []
    for it in sorted(items, key=lambda i: (i.top, i.left)):
        for ln in lines:
            if abs(ln[0].top - it.top) <= body * LINE_TOL:
                ln.append(it)
                break
        else:
            lines.append([it])
    out = []
    for ln in lines:
        ln.sort(key=lambda i: i.left)
        parts, prev = [], None
        for it in ln:
            if prev is not None and not it.img and not prev.img:
                if needs_space(prev, it, max(it.size, body)):
                    parts.append(" ")
            parts.append(with_ruby(it) if inline_ruby else it.text)
            prev = it
        text = "".join(parts).strip()
        size = max((i.size for i in ln if not i.img), default=0.0)
        img = next((i.img for i in ln if i.img), "")
        out.append((ln[0].top, text, bool(img), size))
    return out


def markdown(pages, body: float, media: str, inline_ruby: bool) -> str:
    out: list[str] = []
    for pno, items in pages:
        detect_ruby(items, body)
        keep = [i for i in items if i.img or not i.is_ruby]
        pb = page_body(keep, body)
        lines = to_lines(keep, pb, inline_ruby)
        out.append(f"\n<!-- p.{pno} -->\n")
        prev_top, prev_h, prev_size = None, pb, pb
        for top, text, is_img, size in lines:
            if is_img:
                out.append(f"\n![]({media}/{Path(text).name})\n")
                prev_top = None
                continue
            if not text:
                continue
            if prev_top is not None and (top - prev_top) > max(prev_h, pb) * PARA_GAP:
                out.append("")
            if size >= pb * HEAD1_RATIO and len(text) <= 40:
                out.append(f"## {text}")
            elif size >= pb * HEAD2_RATIO and len(text) <= 40:
                out.append(f"### {text}")
            else:
                out.append(text)
            prev_top, prev_h, prev_size = top, max(size, pb) * 1.15, size
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip() + "\n"


def convert(pdf: Path, out: Path, images: bool, ruby: bool) -> None:
    base = out.with_suffix("")
    img_dir = base.parent / f"{base.name}_media"
    xml = run_pdftohtml(pdf, img_dir if images else None)
    pages = parse(xml)
    if not pages:
        sys.exit(f"{pdf}: 没解析出任何文本(可能是扫描件,需要 OCR)")
    body = body_size(pages)
    n_ruby = sum(detect_ruby(its, body) for _, its in pages)
    media = img_dir.name
    out.write_text(markdown(pages, body, media, inline_ruby=False), encoding="utf-8")
    chars = len(out.read_text(encoding="utf-8"))
    print(f"{pdf.name}: {len(pages)} 页 / 正文字号 {body:g} / 振り仮名 {n_ruby} 块 / {chars} 字 → {out}")
    if ruby:
        r = base.parent / f"{base.name}.ruby.md"
        r.write_text(markdown(pages, body, media, inline_ruby=True), encoding="utf-8")
        print(f"    保留振り仮名 → {r}")


def main() -> int:
    ap = argparse.ArgumentParser(description="PDF → Markdown(去振り仮名)")
    ap.add_argument("pdf", nargs="+")
    ap.add_argument("-o", "--out", help="输出 .md(仅单文件时有效)")
    ap.add_argument("--outdir", default="", help="输出目录(默认与 PDF 同目录)")
    ap.add_argument("--images", action="store_true", help="抽插图到 <名>_media/")
    ap.add_argument("--ruby", action="store_true", help="额外输出保留振り仮名的一份")
    a = ap.parse_args()
    for p in a.pdf:
        pdf = Path(p)
        if not pdf.exists():
            print(f"跳过(不存在): {pdf}", file=sys.stderr)
            continue
        if a.out and len(a.pdf) == 1:
            out = Path(a.out)
        else:
            out = (Path(a.outdir) if a.outdir else pdf.parent) / f"{pdf.stem}.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        convert(pdf, out, a.images, a.ruby)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
