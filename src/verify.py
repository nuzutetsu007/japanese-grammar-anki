#!/usr/bin/env python3
"""阶段 3:自检(verify,同一个模型换个角色)。

用 prompts/verify_v1.md 逐条挑错,结果写入 review(reviewer='self')。
整条 ok → grammar_point.status='verified';否则 'needs_review'。

用法:
    python src/verify.py --limit 3
    python src/verify.py --workers 4
    python src/verify.py --status expanded --ids 1,2
"""
from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    EXPANSION_FIELDS,
    OUT_DIR,
    chat_json,
    connect,
    init_db,
    log_run,
    now,
    read_prompt,
)

PROMPT_VERSION = "verify_v3"
VERDICT_ORDER = {"ok": 0, "doubtful": 1, "wrong": 2}


def collect(conn, gid: int) -> dict:
    fields = {
        r["field"]: {"content": r["content"], "confidence": r["confidence"], "id": r["id"]}
        for r in conn.execute(
            "SELECT * FROM expansion WHERE grammar_id=? AND prompt_version NOT LIKE '%:superseded'"
            " ORDER BY id DESC",
            (gid,),
        )
    }
    examples = [
        {"conn": r["conn"] or "", "ja": r["ja"], "reading": r["reading"], "zh": r["zh"],
         "id": r["id"]}
        for r in conn.execute(
            "SELECT * FROM example WHERE grammar_id=? AND prompt_version NOT LIKE '%:superseded'"
            " ORDER BY id",
            (gid,),
        )
    ]
    gloss_row = conn.execute(
        "SELECT source_gloss FROM book_fact WHERE grammar_id=? ORDER BY id DESC LIMIT 1", (gid,)
    ).fetchone()
    return {"fields": fields, "examples": examples,
            "gloss": (gloss_row["source_gloss"] if gloss_row else "") or ""}


def build_user_prompt(row, bundle) -> str:
    payload = {
        "文法点": row["title_ja"],
        "接续": bundle["fields"].get("connection", {}).get("content"),
        "意思": bundle["fields"].get("meaning", {}).get("content"),
        "汉字写法": bundle["fields"].get("kanji_writing", {}).get("content"),
        "语源": bundle["fields"].get("etymology", {}).get("content"),
        "用法": bundle["fields"].get("usage_note", {}).get("content"),
        "对比": bundle["fields"].get("contrast", {}).get("content"),
        "例句": [
            {"接续": e["conn"], "ja": e["ja"], "reading": e["reading"], "zh": e["zh"]}
            for e in bundle["examples"]
        ],
    }
    if row["reading_romaji"]:
        payload["来源页罗马字读音"] = row["reading_romaji"]
    if bundle.get("gloss"):
        payload["来源页英文释义(权威参考)"] = bundle["gloss"]
    head = ("请审校下面这条文法扩写。注意「来源页英文释义」是来源方给的,如果「意思」「用法」与它明显冲突"
            "(比如来源说 A、扩写说完全无关的 B),至少给 doubtful 并写进 issues。\n"
            if bundle.get("gloss") else "请审校下面这条文法扩写。\n")
    return head + json.dumps(payload, ensure_ascii=False, indent=2)


def valid(data) -> bool:
    return data.get("verdict") in VERDICT_ORDER and isinstance(data.get("reason", ""), str)


def verify_one(row, bundle, system_prompt, model) -> dict:
    return chat_json(
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": build_user_prompt(row, bundle)},
        ],
        model=model,
        validate=valid,
        max_tokens=900,
        expect_out=500,
    )


def save(conn, row, bundle, data) -> str:
    if not bundle["fields"] and not bundle["examples"]:
        # 空壳(从没扩写成功 / 内容被清过)不能被模型一句「ok」变成 verified ——
        # 那样 loop 会以为全部收敛而退出,这条就永远没内容了。打回 'new'
        # 让 expand 重新生成。
        conn.execute("UPDATE grammar_point SET status='new' WHERE id=?", (row["id"],))
        conn.commit()
        print(f"  EMPTY #{row['id']:>3} {row['title_ja']}: 无任何活跃内容,打回 new 重新扩写",
              flush=True)
        return "empty"
    reviewer = f"self:{data.get('_model', '')}"
    overall = data.get("verdict", "doubtful")
    if overall not in VERDICT_ORDER:
        overall = "doubtful"
    field_verdicts = data.get("field_verdicts") or {}
    # 取「所有字段 + 例句」里最严重的一项作为整条状态。
    # 只看 overall 会有漏洞:模型可能 overall=ok 但某个字段判了 wrong,
    # 那样有问题内容会被标成 verified 并进卡片。
    worst = VERDICT_ORDER[overall]
    worst_verdict = overall
    for field in EXPANSION_FIELDS:
        if field not in bundle["fields"]:
            continue
        v = field_verdicts.get(field, overall)
        if v not in VERDICT_ORDER:
            v = overall
        if VERDICT_ORDER[v] > worst:
            worst, worst_verdict = VERDICT_ORDER[v], v
        # expansion 的 self 审核:按字段记
        conn.execute(
            "INSERT INTO review (target_table, target_id, reviewer, verdict, comment, prompt_version)"
            " VALUES (?,?,?,?,?,?)",
            ("expansion", bundle["fields"][field]["id"], reviewer, v,
             data.get("reason", ""), PROMPT_VERSION),
        )
    ev = field_verdicts.get("examples", overall)
    if ev not in VERDICT_ORDER:
        ev = overall
    if bundle["examples"] and VERDICT_ORDER[ev] > worst:
        worst, worst_verdict = VERDICT_ORDER[ev], ev
    for ex in bundle["examples"]:
        conn.execute(
            "INSERT INTO review (target_table, target_id, reviewer, verdict, comment, prompt_version)"
            " VALUES ('example',?,?,?,?,?)",
            (ex["id"], reviewer, ev, data.get("reason", ""), PROMPT_VERSION),
        )
    status = "verified" if worst == 0 else "needs_review"
    conn.execute("UPDATE grammar_point SET status=? WHERE id=?", (status, row["id"]))
    conn.commit()
    return worst_verdict, status, data


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--ids", default="")
    ap.add_argument("--status", default="expanded", help="可逗号分隔多个")
    ap.add_argument("--model", default="",
                    help="留空=用容灾链(GRAMMAR_MODELS),也可写 provider:model")
    ap.add_argument("--workers", type=int, default=3)
    args = ap.parse_args()

    system_prompt = read_prompt(PROMPT_VERSION)
    conn = connect()
    init_db(conn)
    report_dir = OUT_DIR / "verify"
    report_dir.mkdir(parents=True, exist_ok=True)

    sts = [x.strip() for x in args.status.split(",") if x.strip()]
    sql = "SELECT * FROM grammar_point WHERE status IN ({})".format(",".join("?" * len(sts)))
    params: list = list(sts)
    if args.ids:
        ids = [int(x) for x in args.ids.split(",") if x.strip()]
        sql += f" AND id IN ({','.join('?' * len(ids))})"
        params += ids
    sql += " ORDER BY book_order"
    if args.limit:
        sql += f" LIMIT {args.limit}"
    rows = conn.execute(sql, params).fetchall()
    if not rows:
        print(f"[verify] 没有 status={args.status} 的行")
        return 0

    started = now()
    print(f"[verify] {len(rows)} 条待审校,model={args.model or '<容灾链>'}")
    ok = bad = failed = 0
    failures: list[str] = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        jobs = {}
        for r in rows:
            bundle = collect(conn, r["id"])
            jobs[pool.submit(verify_one, r, bundle, system_prompt, args.model)] = (r, bundle)
        for fut in as_completed(jobs):
            row, bundle = jobs[fut]
            try:
                data = fut.result()
                overall, status, _ = save(conn, row, bundle, data)
                (report_dir / f"{row['id']:03d}.json").write_text(
                    json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                if status == "verified":
                    ok += 1
                    print(f"  ok    #{row['id']:>3} {row['title_ja']}")
                else:
                    bad += 1
                    print(f"  {overall:<8} #{row['id']:>3} {row['title_ja']}: {data.get('reason','')}")
            except Exception as e:  # noqa: BLE001
                failed += 1
                failures.append(f"#{row['id']}: {e}")
                print(f"  ERR   #{row['id']:>3} {row['title_ja']}: {e}", file=sys.stderr)

    log_run(conn, "verify", PROMPT_VERSION, started, ok + bad, failed, "; ".join(failures)[:900])
    print(f"[verify] verified={ok} needs_review={bad} failed={failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
