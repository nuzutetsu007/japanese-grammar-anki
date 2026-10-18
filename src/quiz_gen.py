#!/usr/bin/env python3
"""特定技能练习题库:建库 / 教材切块 / 批量出题 / 导入 / 审题 / 补解释 / 统计。

库:db/quiz.sqlite(临时线 —— 考完试直接删);结构:src/quiz_schema.sql。
模型调用复用 common.chat(Google / OpenRouter / Groq 免费额度,配额感知 + 自动容灾):
  * ○× 题输出小(声明 max_tokens 900)→ Groq(OTPM 1000)也能接;
  * 4択 输出大(声明 max_tokens 1900)→ 超过 Groq OTPM,调度器会自动跳过 Groq。

子命令:
    init                                    建库(幂等)
    chunks [--target 700]                   教材 md → source_chunk(幂等)
    gen --kind ox|choice [--out FILE.jsonl] 出题(默认只做没出过的块;直接入库或写 JSONL)
    import FILES...                         JSONL → question 表(按 dedup_key 去重)
    verify [--limit N]                      第二个模型审题
    explain [--limit N]                     给没中文解释的题补一句(优先改写教材解説)
    stats                                   库内统计

规矩(照项目老规矩):
  * 幂等 / 可断点续跑:同 chunk+kind+prompt_version 只出一次;导入去重。
  * uncertain 不进库(JSONL 保留原样供人工看)。
  * AI 内容带 confidence / prompt_version / model。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
import time
import unicodedata
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import OUT_DIR, ROOT, chat_json, log_run, now, read_prompt  # noqa: E402

QUIZ_DB = ROOT / "db" / "quiz.sqlite"
QUIZ_SCHEMA = ROOT / "src" / "quiz_schema.sql"
GUIDE_MD = OUT_DIR / "pdf_md" / "studyguide1_02.md"

KIND_OX, KIND_CHOICE = "ox", "choice"
LETTERS = "ABCD"
PROMPTS = {KIND_OX: "quiz_ox_v1", KIND_CHOICE: "quiz_choice_v2"}
VERIFY_PROMPT = "quiz_verify_v2"
EXPLAIN_PROMPT = "quiz_explain_v1"
CONFS = ("established", "common_claim", "uncertain")

# ○× 一句话输出 → 声明值压到 Groq OTPM(1000)以内,让它也能参与;4択 只有大额度的接得住
MAX_OUT = {KIND_OX: 900, KIND_CHOICE: 1900}
PER_CHUNK = {KIND_OX: 4, KIND_CHOICE: 2}

CHAP_NAMES = {
    1: "試験の概要と学び方", 2: "安全衛生", 3: "品質管理", 4: "一般知識",
    5: "電気に関する法令と規格", 6: "製図", 7: "器具・工具", 8: "機械加工・仕上げ",
    9: "電気、電子基礎理論", 10: "電気、電子作業", 11: "電気、電子応用",
    12: "検査・保全・包装・成形",
}


# ---------------------------------------------------------------- 基础

def _nfkc(s: str) -> str:
    return unicodedata.normalize("NFKC", s or "")


def connect_quiz() -> sqlite3.Connection:
    QUIZ_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(QUIZ_DB, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=8000")
    return conn


def init_quiz(conn: sqlite3.Connection) -> None:
    conn.executescript(QUIZ_SCHEMA.read_text(encoding="utf-8"))
    conn.commit()


def norm_stem(s: str) -> str:
    return re.sub(r"\s+", "", _nfkc(s))


def dedup_key_for(kind: str, stem: str) -> str:
    return hashlib.sha1(f"{kind}|{norm_stem(stem)}".encode("utf-8")).hexdigest()


def usable_sql(table: str = "") -> str:
    p = f"{table}." if table else ""
    return (f"{p}has_figure=0 AND IFNULL({p}confidence,'')<>'uncertain' "
            f"AND IFNULL({p}review_verdict,'')<>'wrong'")


# ---------------------------------------------------------------- 教材切块

def chapter_of(line: str) -> int | None:
    s = _nfkc(line).strip().lstrip("#").strip().replace(" ", "").replace("　", "")
    m = re.match(r"^第([0-9]+)章(.*)$", s)
    if m:
        n = int(m.group(1))
        if n in CHAP_NAMES and m.group(2) == CHAP_NAMES[n].replace(" ", ""):
            return n
    m = re.match(r"^第章(.*)$", s)
    if m:
        for n, nm in CHAP_NAMES.items():
            if m.group(1) == nm.replace(" ", ""):
                return n
    return None


def _is_cjk(ch: str) -> bool:
    return ("\u3000" <= ch <= "\u30ff") or ("\u4e00" <= ch <= "\u9fff") or ("\uff00" <= ch <= "\uffef")


def _join(a: str, b: str) -> str:
    if not a:
        return b
    if not b:
        return a
    if _is_cjk(a[-1]) or _is_cjk(b[0]):
        return a + b
    return a + " " + b


def calibrate_offset(lines: list[str], start: int = 0) -> int:
    """标定「PDF 物理页 − 印刷页码」的固定偏移(前言页数)。

    正文里 <!-- p.N --> 是物理页;每页页脚 ▌… X 第N章… 的 X 是印刷页。
    取开头若干配对 (N − X) 的众数;配不出来退回 9。
    """
    cand: list[int] = []
    last_marker = 0
    for ln in lines[start:start + 5000]:
        mm = re.search(r"<!--\s*p\.\s*([0-9]+)", ln)
        if mm:
            last_marker = int(mm.group(1))
            continue
        hm = re.match(r"^#*\s*▌.*?\s([0-9]+)\s", ln)
        if hm and last_marker:
            d = last_marker - int(hm.group(1))
            if 0 < d < 40:
                cand.append(d)
            if len(cand) >= 30:
                break
    return Counter(cand).most_common(1)[0][0] if cand else 9


def build_chunks(target: int = 700) -> list[dict]:
    hard = int(target * 1.5)
    lines = GUIDE_MD.read_text(encoding="utf-8").splitlines()

    # 正文起点 = 第1章标题(避开书前的目录区,目录里也有「第 章 …」行)
    start = None
    for i, ln in enumerate(lines[:900]):
        s = _nfkc(ln).lstrip("#").strip().replace(" ", "").replace("　", "")
        if s.startswith("第1章試験の概要と学び方"):
            start = i
            break
    if start is None:
        raise RuntimeError("没找到正文起点(第1章),studyguide md 变了?")

    # 页码换算:正文里的 <!-- p.N --> 是 PDF 物理页;每页页脚行 ▌… X 第N章… 里的 X 才是
    # 书里的印刷页码(照片/目录/考试引用都用印刷页)。用页脚现标定偏移,换 PDF 也不怕。
    offset = calibrate_offset(lines, start)

    chapter, page = 0, 0
    cur_page_from, last_page = 0, 0
    in_ex, par = False, ""
    paras: list[str] = []
    n = 0
    ordinal = 0
    out: list[dict] = []

    def flush() -> None:
        nonlocal par, paras, n, ordinal, cur_page_from
        if par:
            paras.append(par)
            par = ""
        text = "\n".join(paras).strip()
        if text and n >= 120 and chapter >= 2:      # 第1章是考试介绍,不出块
            ordinal += 1
            pf = max((cur_page_from or last_page) - offset, 1)
            pt = max(last_page - offset, 1)
            out.append({"chapter": CHAP_NAMES[chapter], "ordinal": ordinal,
                        "page_from": pf, "page_to": pt,
                        "text": text, "n_chars": len(text)})
        paras = []
        n = 0
        cur_page_from = 0

    for ln in lines[start:]:
        s = ln.strip()
        if not s:
            if par:
                paras.append(par)
                par = ""
            if n >= target:
                flush()
            continue
        if s.startswith("<!--"):
            m = re.search(r"p\.\s*([0-9]+)", s)
            if m:
                page = int(m.group(1))
            continue
        if "▌" in s:                                 # 页眉/页脚
            continue
        sn = _nfkc(s).lstrip("#").strip()
        if "製造分野特定技能1号評価試験" in sn and "|" in sn:
            continue
        if re.fullmatch(r"[0-9]+", sn):              # 纯页码
            continue
        if re.match(r"^練習問題\s*[0-9]", sn):        # 练习题块(答案在内,另有解析器)
            in_ex = True
            if par:
                paras.append(par)
                par = ""
            continue
        if in_ex:
            if chapter_of(s) is not None or re.match(r"^#{1,2}\s", s):
                in_ex = False                        # 落到终止行,继续正常处理
            else:
                continue
        c = chapter_of(s)
        if c is not None:
            flush()
            chapter = c
            continue
        if chapter < 2:
            continue
        t = re.sub(r"^#+\s*", "", s).strip()
        if not t:
            continue
        if not par and not cur_page_from:
            cur_page_from = page
        par = _join(par, t)
        last_page = page
        n = sum(len(p) for p in paras) + len(par)
        if n >= hard:
            if par:
                paras.append(par)
                par = ""
            flush()
    flush()
    return out


# ---------------------------------------------------------------- 题目规范化

def _flatten_items(items, depth: int = 3) -> list:
    """摊平模型偶发多包一层的 items: [[{...},{...}]] → [{...},{...}]。"""
    out: list = []
    for it in items or []:
        if isinstance(it, list) and depth > 0:
            out.extend(_flatten_items(it, depth - 1))
        else:
            out.append(it)
    return out


def norm_item(it: dict, kind: str) -> tuple[dict | None, str]:
    if isinstance(it, list) and len(it) == 1 and isinstance(it[0], dict):
        it = it[0]                                 # 模型偶尔把单题包成 [ {...} ] 再塞进 items
    if not isinstance(it, dict):
        return None, "非对象"
    stem = re.sub(r"\s+", " ", str(it.get("stem") or "")).strip()
    if len(stem) < 8:
        return None, "题干太短"
    ans = _nfkc(str(it.get("answer") or "")).strip().upper()[:1]
    if kind == KIND_OX:
        if ans not in ("A", "B"):
            return None, "答案不是A/B"
        choices: list = []
    else:
        ch = it.get("choices")
        if not isinstance(ch, list):
            return None, "choices不是数组"
        ch = [re.sub(r"\s+", " ", str(c)).strip() for c in ch]
        ch = [c for c in ch if c]
        if len(ch) != 4:
            return None, f"选项数={len(ch)}"
        if ans not in ("A", "B", "C", "D"):
            return None, "答案不是A-D"
        choices = ch
    conf = str(it.get("confidence") or "").strip()
    if conf not in CONFS:
        conf = "uncertain"
    try:
        diff = int(it.get("difficulty") or 0)
    except (TypeError, ValueError):
        diff = 0
    diff = min(max(diff, 0), 5)
    expl = str(it.get("explanation_zh") or "").strip()[:300]
    # 模型常把解释写成日语(v2 提示词也压不住):平假名太多的视为「没写」,留给 explain 步骤补中文
    if sum(1 for c in expl if "\u3040" <= c <= "\u309f") >= 5:
        expl = ""
    return {"stem": stem, "choices": choices, "answer": ans,
            "explanation_zh": expl,
            "difficulty": diff, "pages": _nfkc(str(it.get("pages") or "")).strip()[:60],
            "confidence": conf}, ""


def insert_question(conn: sqlite3.Connection, rec: dict) -> int:
    """rec 需含全字段;返回 1=新插入,0=重复被忽略。"""
    cur = conn.execute(
        "INSERT OR IGNORE INTO question (kind, stem, choices, answer, explanation_zh, explanation_ja,"
        " chapter, difficulty, pages, origin, ref, has_figure, confidence, prompt_version, model,"
        " chunk_id, dedup_key, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (rec["kind"], rec["stem"], json.dumps(rec.get("choices") or [], ensure_ascii=False),
         rec["answer"], rec.get("explanation_zh") or "", rec.get("explanation_ja") or "",
         rec.get("chapter") or "", int(rec.get("difficulty") or 0), rec.get("pages") or "",
         rec.get("origin") or "ai", rec.get("ref") or "", int(rec.get("has_figure") or 0),
         rec.get("confidence"), rec.get("prompt_version") or "", rec.get("model") or "",
         rec.get("chunk_id"), dedup_key_for(rec["kind"], rec["stem"]), now(), now()))
    return cur.rowcount


# ---------------------------------------------------------------- 子命令

def cmd_chunks(conn: sqlite3.Connection, a) -> int:
    chunks = build_chunks(a.target)
    ins = 0
    for c in chunks:
        h = hashlib.sha1(f"{c['chapter']}|{c['text']}".encode("utf-8")).hexdigest()
        cur = conn.execute(
            "INSERT OR IGNORE INTO source_chunk (chapter, ordinal, page_from, page_to, text, n_chars, hash, created_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (c["chapter"], c["ordinal"], c["page_from"], c["page_to"], c["text"], c["n_chars"], h, now()))
        ins += cur.rowcount
    conn.commit()
    print(f"[quiz_gen] 切块: 本次解析 {len(chunks)} 块 / 新增入库 {ins} 块")
    for r in conn.execute("SELECT chapter, COUNT(*) n, SUM(n_chars) chars, MIN(page_from) pf, MAX(page_to) pt"
                          " FROM source_chunk GROUP BY chapter ORDER BY MIN(id)"):
        print(f"  {r['chapter']:<12} {r['n']:>3} 块 / {r['chars']:>6} 字 / p.{r['pf']}-{r['pt']}")
    return 0


def gen_user_prompt(ch: dict, per: int) -> str:
    return (f"章:{ch['chapter']} / 页码:{ch['page_from']}-{ch['page_to']} / 出题数:N={per}\n\n"
            f"教材段落:\n{ch['text']}")


def cmd_gen(conn: sqlite3.Connection, a) -> int:
    kind = a.kind
    ver = PROMPTS[kind]
    q = ("SELECT c.*, (SELECT COUNT(*) FROM question q WHERE q.chunk_id=c.id AND q.kind=? AND q.prompt_version=?) AS done"
         " FROM source_chunk c WHERE c.chapter<>?")
    p: list = [kind, ver, CHAP_NAMES[1]]
    if a.chapter:
        q += " AND c.chapter=?"
        p.append(a.chapter)
    if a.chunk:
        q += " AND c.id=?"
        p.append(a.chunk)
    q += " ORDER BY c.chapter, c.ordinal"
    tasks = [dict(r) for r in conn.execute(q, p) if a.redo or not r["done"]]
    if a.limit:
        tasks = tasks[: a.limit]
    if not tasks:
        print("[quiz_gen] 没有要出的块(都出过了?加 --redo)")
        return 0
    system = read_prompt(ver)
    per = a.per_chunk or PER_CHUNK[kind]
    max_tokens = a.max_tokens or MAX_OUT[kind]
    mode = f"JSONL → {a.out}" if a.out else "直接入库"
    print(f"[quiz_gen] {len(tasks)} 块 ×{per} 题 / prompt={ver} / max_tokens={max_tokens} / workers={a.workers} / {mode}")
    if a.dry_run:
        print("---- 第一块 user prompt(截断)----")
        print(gen_user_prompt(tasks[0], per)[:1200])
        return 0

    t0 = time.time()
    started = now()
    ok = skip_unc = failed = 0
    fails: list[str] = []
    outf = open(a.out, "a", encoding="utf-8") if a.out else None

    def work(ch: dict) -> tuple[dict, dict]:
        data = chat_json(
            [{"role": "system", "content": system},
             {"role": "user", "content": gen_user_prompt(ch, per)}],
            max_tokens=max_tokens, expect_out=int(max_tokens * 0.55),
            temperature=a.temperature, chain_spec=a.chain,
            validate=lambda d: isinstance(d.get("items"), list) and bool(d["items"]))
        return ch, data

    try:
        with ThreadPoolExecutor(max_workers=max(1, a.workers)) as ex:
            for fut in [ex.submit(work, ch) for ch in tasks]:
                try:
                    ch, data = fut.result()
                except Exception as e:  # noqa: BLE001
                    failed += 1
                    fails.append(f"{type(e).__name__}: {str(e)[:120]}")
                    print(f"    [fail] {type(e).__name__}: {str(e)[:140]}", flush=True)
                    continue
                model = data.get("_model", "")
                got = 0
                for it in _flatten_items(data.get("items", [])):
                    rec, why = norm_item(it, kind)
                    if rec is None:
                        fails.append(f"无效条目({why}):{str(it)[:80]}")
                        continue
                    if rec["confidence"] == "uncertain":
                        skip_unc += 1
                        continue
                    rec.update({"kind": kind, "chapter": ch["chapter"],
                                "chunk_hash": ch["hash"], "origin": "ai",
                                "prompt_version": ver, "model": model})
                    if outf:
                        outf.write(json.dumps(rec, ensure_ascii=False) + "\n")
                        got += 1
                    else:
                        rec["chunk_id"] = ch["id"]
                        got += insert_question(conn, rec)
                if not outf:
                    conn.commit()
                ok += got
                print(f"    +{got}  (chunk#{ch['id']} {ch['chapter']} p.{ch['page_from']}-{ch['page_to']}"
                      f" model={model} {time.time() - t0:.0f}s)", flush=True)
    finally:
        if outf:
            outf.close()
    log_run(conn, "quiz_gen", ver, started, ok, failed, "; ".join(fails)[:900])
    print(f"[quiz_gen] 完成: 编出 {ok} 题 / 跳过 uncertain {skip_unc} / 失败块 {failed} / {time.time() - t0:.0f}s")
    if outf:
        print(f"    合并进库: .venv/bin/python src/quiz_gen.py import {a.out}")
    return 0 if (ok or skip_unc) else 1


def cmd_import(conn: sqlite3.Connection, a) -> int:
    ins = dup = unc = bad = 0
    for fp in a.files:
        pth = Path(fp)
        if not pth.exists():
            print(f"  ! 文件不存在: {fp}")
            continue
        for ln in pth.read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if not ln:
                continue
            try:
                rec = json.loads(ln)
            except json.JSONDecodeError:
                bad += 1
                continue
            kind = str(rec.get("kind") or "")
            if kind not in (KIND_OX, KIND_CHOICE):
                bad += 1
                continue
            norm, _w = norm_item(rec, kind)
            if norm is None:
                bad += 1
                continue
            if norm["confidence"] == "uncertain":
                unc += 1
                continue
            full = {"kind": kind, **norm,
                    "explanation_ja": str(rec.get("explanation_ja") or ""),
                    "chapter": str(rec.get("chapter") or ""),
                    "origin": str(rec.get("origin") or "ai"),
                    "ref": str(rec.get("ref") or ""),
                    "has_figure": int(rec.get("has_figure") or 0),
                    "prompt_version": str(rec.get("prompt_version") or ""),
                    "model": str(rec.get("model") or "")}
            if rec.get("chunk_hash"):
                row = conn.execute("SELECT id FROM source_chunk WHERE hash=?", (rec["chunk_hash"],)).fetchone()
                full["chunk_id"] = row["id"] if row else None
            n_ins = insert_question(conn, full)
            ins += n_ins
            dup += 1 - n_ins
        conn.commit()
    print(f"[quiz_gen] 导入: 新增 {ins} / 重复 {dup} / 跳过 uncertain {unc} / 无法解析 {bad}")
    return 0 if ins or not bad else 1


def cmd_verify(conn: sqlite3.Connection, a) -> int:
    q = ("SELECT q.id, q.kind, q.stem, q.choices, q.answer, q.explanation_zh, q.chapter,"
         " c.text AS chunk_text FROM question q LEFT JOIN source_chunk c ON c.id=q.chunk_id"
         " WHERE q.review_verdict IS NULL")
    p: list = []
    if a.kind:
        q += " AND q.kind=?"
        p.append(a.kind)
    if a.chapter:
        q += " AND q.chapter=?"
        p.append(a.chapter)
    q += " ORDER BY (q.confidence='established'), q.id LIMIT ?"
    p.append(a.limit)
    rows = [dict(r) for r in conn.execute(q, p)]
    if not rows:
        print("[quiz_gen] 没有要审的题")
        return 0
    system = read_prompt(VERIFY_PROMPT)
    print(f"[quiz_gen] 审题 {len(rows)} 道 / workers={a.workers}")
    t0 = time.time()
    started = now()
    tgt = failed = 0
    stat = {"ok": 0, "doubtful": 0, "wrong": 0}

    def work(row: dict) -> tuple[dict, dict]:
        payload = {"kind": row["kind"], "chapter": row["chapter"],
                   "question": {"stem": row["stem"], "answer": row["answer"]}}
        if row["kind"] == KIND_CHOICE:
            try:
                payload["question"]["choices"] = json.loads(row["choices"] or "[]")
            except json.JSONDecodeError:
                pass
        if row["chunk_text"]:
            payload["textbook_excerpt"] = row["chunk_text"][:1500]
        data = chat_json([{"role": "system", "content": system},
                          {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}],
                         max_tokens=300, expect_out=120, temperature=0.0, chain_spec=a.chain)
        return row, data

    with ThreadPoolExecutor(max_workers=max(1, a.workers)) as ex:
        for fut in [ex.submit(work, r) for r in rows]:
            try:
                row, data = fut.result()
            except Exception as e:  # noqa: BLE001
                failed += 1
                print(f"    [fail] {type(e).__name__}: {str(e)[:140]}", flush=True)
                continue
            verdict = str(data.get("verdict") or "").strip().lower()
            if verdict not in stat:
                verdict = "doubtful"
            conn.execute("UPDATE question SET review_verdict=?, review_model=?, review_version=?,"
                         " review_note=?, updated_at=? WHERE id=?",
                         (verdict, data.get("_model", ""), VERIFY_PROMPT,
                          str(data.get("reason") or "")[:200], now(), row["id"]))
            conn.commit()          # 逐条落盘:中断/被杀也只丢当前这条,且不长时间霸占写锁
            tgt += 1
            stat[verdict] += 1
            print(f"    {verdict:<8} #{row['id']} {row['stem'][:34]}", flush=True)
    conn.commit()
    log_run(conn, "quiz_verify", VERIFY_PROMPT, started, tgt, failed, json.dumps(stat, ensure_ascii=False))
    print(f"[quiz_gen] 审题完成: ok={stat['ok']} doubtful={stat['doubtful']} wrong={stat['wrong']}"
          f" / 失败 {failed} / {time.time() - t0:.0f}s")
    return 0


def cmd_explain(conn: sqlite3.Connection, a) -> int:
    q = ("SELECT id, kind, stem, choices, answer, explanation_ja, chapter FROM question"
         " WHERE (explanation_zh IS NULL OR explanation_zh='')")
    p: list = []
    if a.kind:
        q += " AND kind=?"
        p.append(a.kind)
    if a.origin:
        q += " AND origin=?"
        p.append(a.origin)
    if a.chapter:
        q += " AND chapter=?"
        p.append(a.chapter)
    q += " ORDER BY id LIMIT ?"
    p.append(a.limit)
    rows = [dict(r) for r in conn.execute(q, p)]
    if not rows:
        print("[quiz_gen] 没有要补解释的题")
        return 0
    system = read_prompt(EXPLAIN_PROMPT)
    print(f"[quiz_gen] 补中文解释 {len(rows)} 道 / workers={a.workers}")

    def work(row: dict) -> tuple[dict, dict]:
        head = f"题目:{row['stem']}\n正确答案:{row['answer']}"
        if row["kind"] == KIND_CHOICE:
            try:
                ch = json.loads(row["choices"] or "[]")
                head += "\n选项:" + " / ".join(f"{'ABCD'[i]}.{c}" for i, c in enumerate(ch))
            except (json.JSONDecodeError, IndexError):
                pass
        if row["explanation_ja"]:
            head += f"\n教材日语解说:\n{row['explanation_ja'][:600]}"
        data = chat_json([{"role": "system", "content": system},
                          {"role": "user", "content": head}],
                         max_tokens=220, expect_out=90, temperature=0.2, chain_spec=a.chain)
        return row, data

    t0 = time.time()
    started = now()
    done = failed = 0
    with ThreadPoolExecutor(max_workers=max(1, a.workers)) as ex:
        for fut in [ex.submit(work, r) for r in rows]:
            try:
                row, data = fut.result()
            except Exception as e:  # noqa: BLE001
                failed += 1
                print(f"    [fail] {type(e).__name__}: {str(e)[:140]}", flush=True)
                continue
            txt = str(data.get("explanation_zh") or "").strip()[:200]
            if not txt:
                failed += 1
                continue
            conn.execute("UPDATE question SET explanation_zh=?, explain_model=?, explain_version=?, updated_at=? WHERE id=?",
                         (txt, data.get("_model", ""), EXPLAIN_PROMPT, now(), row["id"]))
            conn.commit()          # 逐条落盘(同上:可断点续跑,不霸锁)
            done += 1
    conn.commit()
    log_run(conn, "quiz_explain", EXPLAIN_PROMPT, started, done, failed, "")
    print(f"[quiz_gen] 完成 {done} / 失败 {failed} / {time.time() - t0:.0f}s")
    return 0


def cmd_shuffle(conn: sqlite3.Connection, a) -> int:
    """重排 choice 题(AI 出的)选项,让正确答案字母在 A~D 上均匀分布。

    * 按题 id 定种子 → 结果可复现;重复跑会再次洗牌(分布仍均匀)。
    * 解释里引用了选项字母的会被清空(留给 explain 按新顺序重写)。
    """
    import random
    ref_re = re.compile(r"选项\s*[ABCD]|選択肢\s*[ABCD]|[「『][ABCD][」』]"
                        r"|[ABCD][、,]\s*[ABCD]|^[ABCD][はがを]")
    rows = conn.execute("SELECT id, choices, answer, explanation_zh FROM question"
                        " WHERE kind=? AND origin='ai' ORDER BY id", (KIND_CHOICE,)).fetchall()
    if not rows:
        print("[quiz_gen] 没有可洗牌的 choice 题")
        return 0
    started = now()
    counts = {c: 0 for c in LETTERS}
    n_ok = n_skip = n_clear = 0
    for r in rows:
        try:
            ch = json.loads(r["choices"] or "[]")
        except json.JSONDecodeError:
            ch = []
        old = LETTERS.find(str(r["answer"] or "").strip().upper())
        if old < 0 or len(ch) != 4:
            n_skip += 1
            continue
        correct = ch[old]
        others = [c for i, c in enumerate(ch) if i != old]
        rng = random.Random(8657 + int(r["id"]))
        best = min(counts.values())
        target = rng.choice([c for c in LETTERS if counts[c] == best])
        idx = LETTERS.index(target)
        rng.shuffle(others)
        new = others[:idx] + [correct] + others[idx:]
        counts[target] += 1
        expl = r["explanation_zh"] or ""
        clear = bool(ref_re.search(expl))
        n_clear += clear
        conn.execute("UPDATE question SET choices=?, answer=?, explanation_zh=?, updated_at=? WHERE id=?",
                     (json.dumps(new, ensure_ascii=False), target, "" if clear else expl, now(), r["id"]))
        n_ok += 1
    conn.commit()
    dist = " ".join(f"{c}:{counts[c]}" for c in LETTERS)
    log_run(conn, "quiz_shuffle", "n/a", started, n_ok, n_skip, f"目标分布 {dist} / 清解释 {n_clear}")
    print(f"[quiz_gen] 洗牌 {n_ok} 题 / 跳过 {n_skip} / 因引用字母清解释 {n_clear}")
    print(f"[quiz_gen] 洗牌后答案分布: {dist}")
    return 0


def cmd_stats(conn: sqlite3.Connection, a) -> int:
    tot = conn.execute("SELECT COUNT(*) FROM question").fetchone()[0]
    usable = conn.execute(f"SELECT COUNT(*) FROM question WHERE {usable_sql()}").fetchone()[0]
    print(f"[quiz_gen] question 共 {tot} / 可用 {usable}")
    for r in conn.execute("SELECT kind, origin, COUNT(*) n FROM question GROUP BY kind, origin ORDER BY kind, origin"):
        print(f"  {r['kind']:<7} {r['origin']:<9} {r['n']}")
    for r in conn.execute("SELECT IFNULL(confidence,'(空)') c, COUNT(*) n FROM question GROUP BY c ORDER BY n DESC"):
        print(f"  confidence {r['c']:<12} {r['n']}")
    for r in conn.execute("SELECT IFNULL(review_verdict,'(未审)') v, COUNT(*) n FROM question GROUP BY v ORDER BY n DESC"):
        print(f"  review     {r['v']:<12} {r['n']}")
    for r in conn.execute(f"SELECT chapter, kind, COUNT(*) n FROM question WHERE {usable_sql()}"
                          " GROUP BY chapter, kind ORDER BY chapter, kind"):
        print(f"  {r['chapter']:<12} {r['kind']:<7} {r['n']}")
    ch = conn.execute("SELECT COUNT(*) total, SUM(EXISTS(SELECT 1 FROM question q WHERE q.chunk_id=c.id)) cov"
                      " FROM source_chunk c").fetchone()
    print(f"  切块 {ch['total']} / 已出过题的块 {ch['cov'] or 0}")
    at = conn.execute("SELECT COUNT(*) n, SUM(correct) c FROM attempt").fetchone()
    print(f"  attempt {at['n']}(正确 {at['c'] or 0})")
    for r in conn.execute("SELECT stage, prompt_version, rows_ok, rows_failed, finished_at FROM run_log ORDER BY id DESC LIMIT 5"):
        print(f"  {r['finished_at']} {r['stage']} {r['prompt_version']} ok={r['rows_ok']} fail={r['rows_failed']}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="特定技能练习题库(临时线)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init")
    pc = sub.add_parser("chunks")
    pc.add_argument("--target", type=int, default=700)

    pg = sub.add_parser("gen")
    pg.add_argument("--kind", default=KIND_OX, choices=[KIND_OX, KIND_CHOICE])
    pg.add_argument("--chapter", default="")
    pg.add_argument("--chunk", type=int, default=0)
    pg.add_argument("--limit", type=int, default=0)
    pg.add_argument("--workers", type=int, default=4)
    pg.add_argument("--per-chunk", type=int, default=0, dest="per_chunk")
    pg.add_argument("--out", default="")
    pg.add_argument("--redo", action="store_true")
    pg.add_argument("--chain", default="")
    pg.add_argument("--temperature", type=float, default=0.6)
    pg.add_argument("--max-tokens", type=int, default=0, dest="max_tokens")
    pg.add_argument("--dry-run", action="store_true")

    pi = sub.add_parser("import")
    pi.add_argument("files", nargs="+")

    pv = sub.add_parser("verify")
    pv.add_argument("--kind", default="", choices=["", KIND_OX, KIND_CHOICE])
    pv.add_argument("--chapter", default="")
    pv.add_argument("--limit", type=int, default=200)
    pv.add_argument("--workers", type=int, default=3)
    pv.add_argument("--chain", default="")

    pe = sub.add_parser("explain")
    pe.add_argument("--origin", default="")
    pe.add_argument("--kind", default="", choices=["", KIND_OX, KIND_CHOICE])
    pe.add_argument("--chapter", default="")
    pe.add_argument("--limit", type=int, default=200)
    pe.add_argument("--workers", type=int, default=4)
    pe.add_argument("--chain", default="")

    sub.add_parser("shuffle")
    sub.add_parser("stats")
    a = ap.parse_args()

    conn = connect_quiz()
    if a.cmd == "init":
        init_quiz(conn)
        n = conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
        print(f"[quiz_gen] 建库完成: {QUIZ_DB}({n} 张表)")
        return 0
    init_quiz(conn)
    if a.cmd == "chunks":
        return cmd_chunks(conn, a)
    if a.cmd == "gen":
        return cmd_gen(conn, a)
    if a.cmd == "import":
        return cmd_import(conn, a)
    if a.cmd == "verify":
        return cmd_verify(conn, a)
    if a.cmd == "shuffle":
        return cmd_shuffle(conn, a)
    if a.cmd == "explain":
        return cmd_explain(conn, a)
    if a.cmd == "stats":
        return cmd_stats(conn, a)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
