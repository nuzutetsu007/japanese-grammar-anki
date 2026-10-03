#!/usr/bin/env python3
"""阶段 3.1:假名注音修复(kana)。

抽检发现:让一个模型同时干「接续判断 + 例句创作 + 整句注音 + 语源」,
最容易崩的就是**整句假名注音**(手紙→たがみ、起こす→こす、拍手→はじゅう…)。
所以把注音拆出来单独做:一次只干这一件事,准确率明显更高。

**单模型直出**(不再双票投票):注音交给最新的 gemini-flash-lite 一把过。
投票只会让弱模型把正确答案投成「不一致」——既慢(卡在等配额上)又不准。
质量留给下一轮 `qc.py` 的规则检查兜底:注音混汉字 / 单独片假名 / 顺序对不上,
qc 都能确定性地抓出来。

用法:
    python3 src/kana.py                  # 全部
    python3 src/kana.py --flagged-only   # 只修「被标记过注音」的(循环里用)
    python3 src/kana.py --ids 1,2,3
    python3 src/kana.py --dry
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import OUT_DIR, chat_json, connect, init_db, log_run, now  # noqa: E402

PROMPT_VERSION = "kana_v2"
RAW_DIR = OUT_DIR / "kana"

# 主模型 + 同家族备份,只为 429/超时时不卡死(不是投票,取先成功的那次)。
# 主模型:"最新 flash lite"。备份同家族,保证读音风格一致。
MODEL_CHAIN = "google:gemini-flash-lite-latest,google:gemini-3.1-flash-lite"

SYSTEM = """你是日语注音工具,只做一件事:把日语句子逐字注成平假名。

规则:
1. 输出 JSON:`{"readings": ["第一句的整句平假名", "第二句的...", ...]}`,顺序和输入完全一致。
2. 必须是**整句平假名**:所有汉字都写成读音,片假名保留片假名,数字写成读音。
3. 助词按实际读音写:は→わ、へ→え、を→を。
4. 逐字核对,不要漏字、不要多字、不要改词。特别注意:
   - 「入る」在「部屋に入る」里是「はいる」,在「気に入る」里也是「はいる」
   - 「起こす」是「おこす」(不是「こす」)  「遅れた」是「おくれた」(不是「おそれた」)
   - 「手紙」是「てがみ」「拍手」是「はくしゅ」「顔色」是「かおいろ」
   - 「今月」是「こんげつ」「生活」是「せいかつ」「学問」是「がくもん」
5. 不要输出任何解释、不要 markdown 围栏。"""

_KANJI = re.compile(r"[\u4e00-\u9fff]")


def readings(title: str, sentences: list[str]) -> tuple[list[str], str]:
    """给一批句子注音,返回 (readings, 实际用到的模型)。"""
    user = (f"文法点:「{title}」\n\n"
            "请给下面 %d 个句子注音:\n" % len(sentences)
            + "\n".join(f"{i}. {s}" for i, s in enumerate(sentences, 1)))
    data = chat_json(
        [{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}],
        chain_spec=MODEL_CHAIN, max_tokens=1600, expect_out=1000,
        validate=lambda d: isinstance(d.get("readings"), list),
    )
    # chat_json 会把实际成功的 "provider:model" 塞在 _model(链上可能容灾换过)
    return [str(x) for x in data["readings"]], str(data.get("_model") or MODEL_CHAIN)


def suspicious(reading: str) -> str:
    """本地快检(便宜、确定性):注音里不该出现汉字。有问题返回描述,没问题返回 ""。

    更细的检查(顺序对不上、单独片假名)交给下一轮 qc.py,那里已经是确定性的规则。
    """
    r = (reading or "").strip()
    if not r:
        return "注音为空"
    m = _KANJI.search(r)
    if m:
        return f"注音里还有汉字「{m.group()}」"
    return ""


def process(row, sentences: list[str]) -> dict:
    gid = row["id"]
    if not sentences:
        return {"gid": gid, "skip": True}
    out, model = readings(row["title_ja"], sentences)
    ok: list[tuple[int, str]] = []
    bad: list[tuple[int, str, str, str]] = []
    for i, ja in enumerate(sentences):
        r = out[i] if i < len(out) else ""
        why = suspicious(r)
        if why:
            bad.append((i, ja, r, why))
        else:
            ok.append((i, r))
    return {"gid": gid, "model": model, "ok": ok, "bad": bad}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ids", default="")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--flagged-only", action="store_true",
                    help="只处理「注音被标记过」的条目(循环里用这个,避免每轮全量空烧额度)")
    ap.add_argument("--skip-stuck", action="store_true", help="跳过 loop_state.stuck=1 的条目")
    args = ap.parse_args()

    conn = connect()
    init_db(conn)
    RAW_DIR.mkdir(parents=True, exist_ok=True)

    sql = ("SELECT * FROM grammar_point WHERE status IN ('expanded','verified','needs_review')")
    params: list = []
    if args.flagged_only:
        # 只挑「最新的审核意见里提到注音/假名/reading」的条目,避免每轮全量重跑
        sql += """ AND id IN (
            SELECT e.grammar_id FROM review r
              JOIN expansion e ON r.target_table='expansion' AND r.target_id=e.id
             WHERE r.verdict IN ('doubtful','wrong')
               AND e.prompt_version NOT LIKE '%:superseded'
               AND (r.comment LIKE '%注音%' OR r.comment LIKE '%假名%'
                    OR r.comment LIKE '%reading%' OR r.comment LIKE '%读音%')
               AND r.id = (SELECT MAX(r2.id) FROM review r2
                            WHERE r2.target_table=r.target_table AND r2.target_id=r.target_id)
        ) OR id IN (
            SELECT x.grammar_id FROM review r
              JOIN example x ON r.target_table='example' AND r.target_id=x.id
             WHERE r.verdict IN ('doubtful','wrong')
               AND x.prompt_version NOT LIKE '%:superseded'
               AND (r.comment LIKE '%注音%' OR r.comment LIKE '%假名%'
                    OR r.comment LIKE '%reading%' OR r.comment LIKE '%读音%')
               AND r.id = (SELECT MAX(r2.id) FROM review r2
                            WHERE r2.target_table=r.target_table AND r2.target_id=r.target_id)
        )"""
    if args.skip_stuck:
        sql += " AND id NOT IN (SELECT grammar_id FROM loop_state WHERE stuck=1)"
    if args.ids:
        ids = [int(x) for x in args.ids.split(",") if x.strip()]
        sql = f"SELECT * FROM grammar_point WHERE id IN ({','.join('?' * len(ids))})"
        params = ids
    sql += " ORDER BY book_order"
    if args.limit:
        sql += f" LIMIT {args.limit}"
    rows = conn.execute(sql, params).fetchall()

    started = now()
    print(f"[kana] {len(rows)} 条文法点,模型 = {MODEL_CHAIN}")
    fixed = bad = failed = 0
    # sqlite 连接不能跨线程,所以句子在主线程取好再传给 worker
    sents: dict[int, list[str]] = {}
    for r in rows:
        sents[r["id"]] = [x["ja"] for x in conn.execute(
            "SELECT ja FROM example WHERE grammar_id=? AND prompt_version NOT LIKE '%:superseded'"
            " ORDER BY id", (r["id"],))]

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = {pool.submit(process, r, sents[r["id"]]): r for r in rows}
        done = 0
        for fut in as_completed(futs):
            row = futs[fut]
            gid = row["id"]
            done += 1
            try:
                res = fut.result()
            except Exception as e:  # noqa: BLE001
                failed += 1
                print(f"  ERR #{gid:>3} {row['title_ja']}: {e}", file=sys.stderr, flush=True)
                continue
            if res.get("skip"):
                continue
            if args.dry:
                if res["bad"]:
                    bad += 1
                continue

            ex_ids = [r["id"] for r in conn.execute(
                "SELECT id FROM example WHERE grammar_id=? AND prompt_version NOT LIKE"
                " '%:superseded' ORDER BY id", (gid,))]
            wrote = 0
            for i, rd in res["ok"]:
                if i < len(ex_ids):
                    conn.execute("UPDATE example SET reading=? WHERE id=?", (rd, ex_ids[i]))
                    fixed += 1
                    wrote += 1
            if wrote and row["status"] == "verified":
                # 内容被改写了,之前的 verified 就作废了 —— 回到 needs_review
                # 让 verify 重新审,而不是带着新注音停在「已验证」。
                conn.execute("UPDATE grammar_point SET status='needs_review' WHERE id=?", (gid,))
            for i, ja, rd, why in res["bad"]:
                bad += 1
                if i < len(ex_ids):
                    conn.execute(
                        "INSERT INTO review (target_table, target_id, reviewer, verdict, comment,"
                        " prompt_version) VALUES ('example', ?, 'kana:gen', 'doubtful', ?, ?)",
                        (ex_ids[i],
                         f"【注音生成可疑】第{i+1}句「{ja}」→「{rd or '空'}」:{why}。请重写这一句的注音。",
                         PROMPT_VERSION))
            conn.commit()
            (RAW_DIR / f"{gid:03d}.json").write_text(
                json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
            if done % 20 == 0:
                print(f"  … {done}/{len(rows)}  已修正 {fixed} 句 / 有问题 {bad} 条",
                      flush=True)

    log_run(conn, "kana", PROMPT_VERSION, started, fixed, bad,
            f"{len(rows)} 条 / 修正 {fixed} 句 / 可疑 {bad} 句;模型 {MODEL_CHAIN}")
    print(f"[kana] 修正 {fixed} 句注音;{bad} 句可疑;失败 {failed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
