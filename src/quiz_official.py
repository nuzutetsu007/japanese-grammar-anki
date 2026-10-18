#!/usr/bin/env python3
"""官方样题 + 教材練習問題 → JSONL(纯解析,不调模型)。

    .venv/bin/python src/quiz_official.py              # 两份都出
    .venv/bin/python src/quiz_official.py sample       # 只出官方样题
    .venv/bin/python src/quiz_official.py textbook     # 只出教材練習問題
    .venv/bin/python src/quiz_gen.py import data/out/quiz_official.jsonl data/out/quiz_textbook.jsonl

- 官方样题(sample1_02.md):学科 30 道 ○×(正答在卷末【正答】表),
  実技 10 道 4択;带图的题 has_figure=1(不进可用池,但留库)。
- 教材練習問題(studyguide1_02.md):只收「正しい場合はA、間違っている場合はB」型、
  且答案全在 A/B、条目①②③连号的块;每题附日语解説(explanation_ja),
  中文解释(explanation_zh)留给 quiz_gen.py explain 步骤。

规矩:全角数字/字母先 NFKC;页码换算成印刷页(用 quiz_gen.calibrate_offset 现场标定);
解析不了的一律带原因跳过并打印,绝不硬猜。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import OUT_DIR  # noqa: E402
from quiz_gen import CHAP_NAMES, calibrate_offset  # noqa: E402

SAMPLE_MD = OUT_DIR / "pdf_md" / "sample1_02.md"
GUIDE_MD = OUT_DIR / "pdf_md" / "studyguide1_02.md"

CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"
# 依赖图/照片/图表的题:网页只有文字,这类打 has_figure=1 不进可用池
FIG_RE = re.compile(r"下の図|下に示す|下の写真|下記の図|次の図|図記号|回路図|タイムチャート|配線図|グラフ|写真の|写真は|a\)")


def _nfkc(s: str) -> str:
    return unicodedata.normalize("NFKC", s or "")


def _strip_md(s: str) -> str:
    return re.sub(r"^#+\s*", "", s).strip()


def _is_alnum1(c: str) -> bool:
    """半角/全角字母或数字(用来识别「問題５４Ｓとは」这种粘连)。"""
    return ("0" <= c <= "9") or ("A" <= c <= "Z") or ("a" <= c <= "z") \
        or ("０" <= c <= "９") or ("Ａ" <= c <= "Ｚ") or ("ａ" <= c <= "ｚ")


def _body(s: str) -> str:
    """正文润色:①②→(1)(2) 留出条目边界,再做 NFKC(全角数字/字母转半角)。"""
    return _nfkc(re.sub(r"[①-⑳]", lambda m: f"({CIRCLED.index(m.group(0)) + 1}) ", s))


def make(kind: str, stem: str, answer: str, *, choices=None, origin: str, ref: str,
         has_figure: int, chapter: str = "", pages: str = "", explanation_ja: str = "",
         prompt_version: str) -> dict:
    return {"kind": kind, "stem": stem, "choices": choices or [], "answer": answer,
            "explanation_zh": "", "explanation_ja": explanation_ja, "chapter": chapter,
            "difficulty": 0, "pages": pages, "origin": origin, "ref": ref,
            "has_figure": has_figure, "confidence": "established",
            "prompt_version": prompt_version, "model": ""}


# ---------------------------------------------------------------- 官方样题

def parse_sample(path: Path) -> tuple[list[dict], list[str]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    notes: list[str] = []

    # 卷末【正答】表:一行里可能排着几组「問題 N X」
    answers: dict[int, str] = {}
    in_key = False
    for s in lines:
        if "【正答】" in s:
            in_key = True
            continue
        if in_key:
            for m in re.finditer(r"問題\s*([0-9]+)\s*([A-D])", _nfkc(s)):
                answers[int(m.group(1))] = m.group(2)

    # 切题块:问题号行起头,下一个问题号行截止
    blocks: dict[int, list[str]] = {}
    cur = None
    for s in lines:
        if "【正答】" in s:
            break
        t = _strip_md(s)
        if not t or t.startswith("<!--"):
            continue
        tn = _nfkc(t)
        if re.search(r"問題\s*[0-9]+\s*から問題", tn):   # 「問題1 から問題30 について…」指令行
            continue
        if re.fullmatch(r"[0-9]+", tn):                 # 页码
            continue
        if re.fullmatch(r"[()\s]+", tn):                # 孤零零的括号(答题栏残留)
            continue
        m = re.match(r"^問題([0-9０-９]+)(\s*)(.*)$", t)
        if m:
            digits, gap, rest = m.group(1), m.group(2), m.group(3)
            # 「問題５４Ｓとは…」= 問題5 + 正文以「4S」开头:OCR 丢空格时数字和正文粘连
            if not gap and rest and len(digits) > 1 and _is_alnum1(rest[0]):
                digits, rest = digits[:-1], digits[-1] + rest
            cur = int(_nfkc(digits))
            rest = _body(rest).strip()
            blocks[cur] = [rest] if rest else []
            continue
        if cur is not None:
            blocks[cur].append(_body(t))

    out: list[dict] = []
    for n in sorted(blocks):
        body = "".join(blocks[n]).strip()   # 日语断行无需空格,直接粘
        ans = answers.get(n)
        if not ans or len(body) < 8:
            notes.append(f"sample 問題{n}: 缺正答或题干过短,跳过")
            continue
        fig = 1 if FIG_RE.search(body) else 0
        if n <= 30:                          # 学科 ○×
            out.append(make("ox", body, ans, origin="official", ref=f"問題{n}",
                            has_figure=fig, prompt_version="official_2025"))
            continue
        # 実技 4択:按 A. B. C. D. 标签切选项
        marks = [(mm.start(), mm.group(1)) for mm in re.finditer(r"([A-D])\.", body)]
        seq: list[tuple[int, str]] = []
        for want in "ABCD":
            nxt = next(((p, c) for p, c in marks if c == want and (not seq or p > seq[-1][0])), None)
            if nxt is None:
                break
            seq.append(nxt)
        if len(seq) != 4:
            notes.append(f"sample 問題{n}: 选项切分失败({len(seq)}/4),跳过")
            continue
        stem = body[: seq[0][0]].strip()
        stem = re.sub(r"(選びなさい。)\s*選択肢\s*$", r"\1", stem)   # 去掉尾部「選択肢」残留
        stem = re.sub(r"解答しなさい。\s*選択肢\s*$", "解答しなさい。", stem)
        if len(stem) < 8:
            notes.append(f"sample 問題{n}: 题干清洗后过短,跳过")
            continue
        chs = []
        for i, (p, _c) in enumerate(seq):
            end = seq[i + 1][0] if i + 1 < 4 else len(body)
            chs.append(body[p + 2:end].strip())
        if any(not c for c in chs):
            notes.append(f"sample 問題{n}: 有空选项,跳过")
            continue
        out.append(make("choice", stem, ans, choices=chs, origin="official",
                        ref=f"問題{n}", has_figure=fig, prompt_version="official_2025"))
    return out, notes


# ---------------------------------------------------------------- 教材練習問題

def parse_textbook(path: Path) -> tuple[list[dict], list[str]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    offset = calibrate_offset(lines, 0)
    notes: list[str] = []

    heads: list[tuple[int, int, int]] = []
    for i, s in enumerate(lines):
        t = _strip_md(s)
        m = re.match(r"^練習問題\s*([0-9]+)\s*-\s*([0-9]+)", t)
        if m:
            heads.append((i, int(m.group(1)), int(m.group(2))))
    if not heads:
        return [], ["没找到任何『練習問題 X-Y』标题,md 变了?"]

    out: list[dict] = []
    for hi, (start_i, x, y) in enumerate(heads):
        end_i = heads[hi + 1][0] if hi + 1 < len(heads) else len(lines)
        items: dict[int, list[str]] = {}
        choices: dict[int, list[str]] = {}
        answers: list[str] = []
        expls: dict[int, list[str]] = {}
        instr_kind = ""            # "ox" / "choice"
        instr_lines: list[str] = []
        blob: list[str] = []
        instr_tail = False
        phase = "items"
        cur_item = cur_expl = cur_choice = None
        page = 0
        for s in reversed(lines[:start_i]):               # 块前最近一个页码标记
            mm = re.search(r"<!--\s*p\.\s*([0-9]+)", s)
            if mm:
                page = max(int(mm.group(1)) - offset, 1)
                break
        for s in lines[start_i + 1:end_i]:
            t = _strip_md(s)
            if not t:
                continue
            if t.startswith("<!--"):
                mm = re.search(r"p\.\s*([0-9]+)", t)
                if mm:
                    page = max(int(mm.group(1)) - offset, 1)
                continue
            if "▌" in t:                                  # 页脚
                continue
            sn = _nfkc(t)
            if re.match(r"^第[0-9]+章", sn):              # 意外越到下一章,收工
                break
            if re.fullmatch(r"[0-9]+", sn):               # 页码
                continue
            if phase == "items":
                if sn.startswith("■"):                    # 指示行:认 ○× 型 / 4択 型
                    instr_lines.append(sn.lstrip("■").strip())
                    if re.search(r"正し[いく]場合は\s*A[、,，]?\s*間違っている場合は\s*B", sn):
                        instr_kind = "ox"
                    elif re.search(r"選択肢A~D|正しい組み合わせを選びなさい", sn):
                        instr_kind = "choice"
                    instr_tail = True
                    continue
                if instr_tail:                            # 指示行的续行
                    if re.match(r"^解説", sn) or re.match(r"^[A-D]\.", sn) or t[0] in CIRCLED:
                        instr_tail = False                # 落到下面正常处理
                    else:
                        if instr_kind:
                            instr_lines.append(sn)
                        continue
                if re.match(r"^解説", sn):
                    phase = "explain"
                    continue
                if t[0] in CIRCLED:
                    idx = CIRCLED.index(t[0])
                    rest = re.sub(r"^[（(]\s*[)）]\s*", "", t[1:].strip())
                    items[idx] = [rest] if rest else []
                    cur_item = idx
                    continue
                if instr_kind == "choice":
                    m3 = re.match(r"^([A-D])\.\s*(.*)$", sn)
                    if m3:
                        idx = "ABCD".index(m3.group(1))
                        choices[idx] = [m3.group(2).strip()] if m3.group(2).strip() else []
                        cur_choice = idx
                        continue
                    if cur_choice is not None:
                        choices[cur_choice].append(sn)
                    continue
                if cur_item is not None:
                    items[cur_item].append(t)
                continue
            # explain 阶段
            m2 = re.match(r"^(?:答|解答)\s*[:：]\s*([A-D])", sn)
            if m2:
                answers.append(m2.group(1))
                if instr_kind == "choice":
                    break                                 # 4択:一个答案就收工
                if items and len(answers) >= len(items):
                    break                                 # 答案齐了,后面(補足等)不要
                continue
            if sn.startswith("補足"):
                break
            if instr_kind == "choice":
                blob.append(sn)                           # 4択:整体一段解説
                continue
            if t[0] in CIRCLED:
                idx = CIRCLED.index(t[0])
                expls[idx] = [t[1:].strip()]
                cur_expl = idx
                continue
            if cur_expl is not None:
                expls[cur_expl].append(t)

        if instr_kind == "choice":                        # 4択 块
            ctexts = ["".join(choices.get(i, [])).strip() for i in range(4)]
            stem = "".join(instr_lines).strip()
            if any(not c for c in ctexts) or len(answers) != 1 or len(stem) < 8:
                notes.append(f"練習問題 {x}-{y}: 跳过 —— 4択块解析不全"
                             f"(选项{sum(1 for c in ctexts if c)}/4,答案{len(answers)})")
                continue
            fig_text = stem + "".join(ctexts) + "".join(blob)
            out.append(make("choice", stem, answers[0], choices=ctexts, origin="textbook",
                            ref=f"練習問題 {x}-{y}",
                            has_figure=1 if FIG_RE.search(fig_text) else 0,
                            chapter=CHAP_NAMES.get(x, ""), pages=str(page) if page else "",
                            explanation_ja="".join(blob).strip(), prompt_version="textbook"))
            continue

        idxs = sorted(items)
        why = ""
        if instr_kind != "ox":
            why = "不是「正しいA/間違っているB」型,也不是 4択 型"
        elif len(items) < 2:
            why = f"条目数不足({len(items)})"
        elif idxs != list(range(len(idxs))):
            why = "条目编号不连号"
        elif len(answers) != len(items):
            why = f"答案数({len(answers)})≠条目数({len(items)})"
        elif any(a not in ("A", "B") for a in answers):
            why = "含 C/D(图题)"
        if why:
            notes.append(f"練習問題 {x}-{y}: 跳过 —— {why}")
            continue

        for i, idx in enumerate(idxs):
            stem = "".join(items[idx]).strip()
            stem = re.sub(r"^[（(]\s*[)）]\s*", "", stem)
            if len(stem) < 6:
                notes.append(f"練習問題 {x}-{y}・問{i + 1}: 题干过短,跳过")
                continue
            expl = "".join(expls.get(idx, [])).strip()
            out.append(make("ox", stem, answers[i], origin="textbook",
                            ref=f"練習問題 {x}-{y}・問{i + 1}",
                            has_figure=1 if FIG_RE.search(stem) else 0,
                            chapter=CHAP_NAMES.get(x, ""),
                            pages=str(page) if page else "",
                            explanation_ja=expl, prompt_version="textbook"))
    return out, notes


def main() -> int:
    ap = argparse.ArgumentParser(description="官方样题 + 教材練習問題 → JSONL")
    ap.add_argument("what", nargs="?", default="all", choices=["all", "sample", "textbook"])
    ap.add_argument("--out-dir", default=str(OUT_DIR))
    a = ap.parse_args()
    out_dir = Path(a.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    todo = []
    if a.what in ("all", "sample"):
        todo.append(("sample", parse_sample, SAMPLE_MD, out_dir / "quiz_official.jsonl"))
    if a.what in ("all", "textbook"):
        todo.append(("textbook", parse_textbook, GUIDE_MD, out_dir / "quiz_textbook.jsonl"))

    rc = 0
    for name, fn, src, dst in todo:
        if not src.exists():
            print(f"[quiz_official] 源文件不存在: {src}")
            rc = 1
            continue
        records, notes = fn(src)
        with open(dst, "w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        kinds: dict[str, int] = {}
        for r in records:
            kinds[r["kind"]] = kinds.get(r["kind"], 0) + 1
        usable = sum(1 for r in records if not r["has_figure"])
        print(f"[quiz_official] {name}: {len(records)} 题 → {dst}")
        print(f"    分布 {kinds} / 可用(无图) {usable}")
        for n in notes[:40]:
            print(f"    - {n}")
        if len(notes) > 40:
            print(f"    … 还有 {len(notes) - 40} 条")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
