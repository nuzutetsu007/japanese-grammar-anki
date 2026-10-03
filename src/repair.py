#!/usr/bin/env python3
"""阶段 3.5:修复(repair)—— 只重跑被审校者标 doubtful/wrong 的行。

把「当前扩写 + 审校者指出的具体问题」一起交给模型,要它只修问题并返回同样的 JSON。
旧的 expansion / example 行标成 `:superseded` 保留(prompt_version 可追溯),新行入库,
状态回到 'expanded' 等下一次 verify。

用法:
    python src/repair.py --limit 3            # 试跑
    python src/repair.py --workers 4          # 修复全部 needs_review
    python src/repair.py --ids 2,4,5
"""
from __future__ import annotations

import argparse
import json
import os
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
from expand import norm_field, norm_reading  # noqa: E402

PROMPT_VERSION = "repair_v3"
MAX_EXAMPLES = int(os.environ.get("GRAMMAR_MAX_EXAMPLES", "8"))
RAW_DIR = OUT_DIR / "repair"


def collect_issues(conn, gid: int) -> list[str]:
    """只收集**当前还生效的**目标上的**最新一条**审核意见。

    两个坑:
      * 旧版本(已 :superseded)的目标上的意见不能再用 —— 内容已经换过一轮了;
      * 同一个目标反复审过多次时,只取最新那次(否则旧问题会被反复喂给模型)。
    """
    rows = conn.execute(
        """
        SELECT r.verdict, r.reviewer, r.comment
          FROM review r
          LEFT JOIN expansion e ON r.target_table='expansion' AND r.target_id=e.id
          LEFT JOIN example   x ON r.target_table='example'   AND r.target_id=x.id
         WHERE (e.grammar_id=? OR x.grammar_id=?)
           AND r.verdict IN ('doubtful','wrong')
           AND COALESCE(e.prompt_version,'') NOT LIKE '%:superseded'
           AND COALESCE(x.prompt_version,'') NOT LIKE '%:superseded'
           AND r.id = (SELECT MAX(r2.id) FROM review r2
                        WHERE r2.target_table = r.target_table
                          AND r2.target_id   = r.target_id
                          AND r2.reviewer    = r.reviewer)
        """,
        (gid, gid),
    ).fetchall()
    seen, out = set(), []
    for r in rows:
        c = (r["comment"] or "").strip()
        if c and c not in seen:
            seen.add(c)
            out.append(f"[{r['verdict']}] {c}")
    return out


def collect_current(conn, gid: int) -> dict:
    fields = {}
    for e in conn.execute(
        "SELECT * FROM expansion WHERE grammar_id=? AND prompt_version NOT LIKE '%:superseded'"
        " ORDER BY id",
        (gid,),
    ):
        fields[e["field"]] = {"content": e["content"], "confidence": e["confidence"]}
    fields["examples"] = [
        {"conn": r["conn"] or "", "ja": r["ja"], "reading": r["reading"], "zh": r["zh"]}
        for r in conn.execute(
            "SELECT * FROM example WHERE grammar_id=? AND prompt_version NOT LIKE '%:superseded'"
            " ORDER BY id",
            (gid,),
        )
    ]
    return fields


def build_user_prompt(row, current: dict, issues: list[str]) -> str:
    return (
        f"文法点:「{row['title_ja']}」({row['chapter']} 第{row['page_ref']}页)\n\n"
        "## 现在的扩写\n"
        + json.dumps(current, ensure_ascii=False, indent=2)
        + "\n\n## 审校者指出的问题(必须全部解决)\n"
        + "\n".join(f"- {i}" for i in issues)
        + "\n\n请输出修正后的 JSON。"
    )


def repair_one(row, current, issues, system_prompt, model) -> dict:
    return chat_json(
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": build_user_prompt(row, current, issues)},
        ],
        model=model,
        validate=lambda d: any(f in d for f in EXPANSION_FIELDS)
        and isinstance(d.get("examples"), list),
        max_tokens=2600,
        expect_out=1800,
    )


def save(conn, row, data) -> tuple[int, int]:
    gid = row["id"]
    model = data.get("_model", "unknown")

    # 先解析新内容;一个字段/例句都没有就直接失败,别把旧内容作废掉
    # (踩过:#260「上で」被清空)。
    fields: list[tuple[str, str, str]] = []
    for field in EXPANSION_FIELDS:
        content, conf = norm_field(data.get(field))
        if content is not None:
            fields.append((field, content, conf))
    examples: list[tuple[str, str, str, str]] = []
    for ex in (data.get("examples") or [])[:MAX_EXAMPLES]:
        ja = (ex.get("ja") or "").strip()
        if ja:
            examples.append((ja, norm_reading(ex.get("reading")),
                             (ex.get("zh") or "").strip(), (ex.get("conn") or "").strip()))
    if not fields and not examples:
        raise RuntimeError("修复结果里没有任何可用字段/例句,保留旧内容不动")

    conn.execute(
        "UPDATE expansion SET prompt_version=prompt_version||':superseded' WHERE grammar_id=?"
        " AND prompt_version NOT LIKE '%:superseded'",
        (gid,),
    )
    conn.execute(
        "UPDATE example SET prompt_version=prompt_version||':superseded' WHERE grammar_id=?"
        " AND prompt_version NOT LIKE '%:superseded'",
        (gid,),
    )
    for field, content, conf in fields:
        conn.execute(
            "INSERT INTO expansion (grammar_id, field, content, confidence, model, prompt_version)"
            " VALUES (?,?,?,?,?,?)",
            (gid, field, content, conf, model, PROMPT_VERSION),
        )
    for ja, reading, zh, conn_label in examples:
        conn.execute(
            "INSERT INTO example (grammar_id, ja, reading, zh, conn, origin, verified, model,"
            " prompt_version) VALUES (?,?,?,?,?, 'ai', 0, ?, ?)",
            (gid, ja, reading, zh, conn_label, model, PROMPT_VERSION),
        )
    conn.execute("UPDATE grammar_point SET status='expanded' WHERE id=?", (gid,))
    conn.commit()
    return len(fields), len(examples)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--ids", default="")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--include-stuck", action="store_true",
                    help="默认跳过 loop_state.stuck=1 的条目(那些交给人工)")
    ap.add_argument("--model", default="", help="留空=用容灾链(GRAMMAR_MODELS)")
    args = ap.parse_args()

    system_prompt = read_prompt(PROMPT_VERSION)
    conn = connect()
    init_db(conn)
    RAW_DIR.mkdir(parents=True, exist_ok=True)

    sql = "SELECT * FROM grammar_point WHERE status IN ('needs_review','expanded')"
    params: list = []
    if not args.include_stuck:
        sql += " AND id NOT IN (SELECT grammar_id FROM loop_state WHERE stuck=1)"
    if args.ids:
        ids = [int(x) for x in args.ids.split(",") if x.strip()]
        sql = f"SELECT * FROM grammar_point WHERE id IN ({','.join('?' * len(ids))})"
        params += ids
    sql += " ORDER BY book_order"
    if args.limit:
        sql += f" LIMIT {args.limit}"
    rows = conn.execute(sql, params).fetchall()

    jobs = []
    for r in rows:
        issues = collect_issues(conn, r["id"])
        if not issues:
            continue
        jobs.append((r, collect_current(conn, r["id"]), issues))

    if not jobs:
        print("[repair] 没有被标记问题的行")
        return 0

    started = now()
    print(f"[repair] {len(jobs)} 条待修复,model={args.model or '<容灾链>'},workers={args.workers}")
    ok = failed = 0
    failures: list[str] = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(repair_one, r, cur, iss, system_prompt, args.model): r
                   for r, cur, iss in jobs}
        for fut in as_completed(futures):
            row = futures[fut]
            try:
                data = fut.result()
                nf, ne = save(conn, row, data)
                (RAW_DIR / f"{row['id']:03d}.json").write_text(
                    json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
                ok += 1
                print(f"  ok  #{row['id']:>3} {row['title_ja']}  fields={nf} examples={ne}")
            except Exception as e:  # noqa: BLE001
                failed += 1
                failures.append(f"#{row['id']}: {e}")
                print(f"  ERR #{row['id']:>3} {row['title_ja']}: {e}", file=sys.stderr)

    log_run(conn, "repair", PROMPT_VERSION, started, ok, failed, "; ".join(failures)[:900])
    print(f"[repair] 完成 {ok},失败 {failed}")
    print("[repair] 接着重新自检:python src/verify.py --workers 4")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
