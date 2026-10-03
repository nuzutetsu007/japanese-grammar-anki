#!/usr/bin/env python3
"""阶段 4:外部模型校准。

  export:导出 review_batch.json(每条待审内容 + 空的 verdict 字段)拿给其他模型审。
  import:把外部模型填好的结果导回来,写入 review(reviewer='model:<name>')。
  stats :统计多个审查者意见不一致的条目 → status='needs_review'。

用法:
    python src/review_batch.py export --out data/out/review_batch.json
    python src/review_batch.py import data/out/review_batch_done.json --reviewer model:gpt-5.1
    python src/review_batch.py stats
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    EXPANSION_FIELDS,
    OUT_DIR,
    connect,
    init_db,
    log_run,
    now,
)

VERDICT_ORDER = {"ok": 0, "doubtful": 1, "wrong": 2}


def fingerprint(fields: dict, examples: list[dict]) -> str:
    """内容指纹:用来确认导入的批量审核结果对应的还是同一批内容。

    光靠 grammar_id 不够 —— 重跑 expand/repair 后内容会变,
    旧批次的结论不能再套到新内容上。
    """
    payload = {
        "fields": {k: (v or {}).get("content") for k, v in (fields or {}).items()},
        "examples": [
            {k: e.get(k) for k in ("ja", "reading", "zh")} for e in (examples or [])
        ],
    }
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(blob.encode("utf-8")).hexdigest()


def export(conn, out_path: Path, statuses: tuple[str, ...]) -> int:
    rows = conn.execute(
        "SELECT * FROM grammar_point WHERE status IN ({}) ORDER BY book_order".format(
            ",".join("?" * len(statuses))
        ),
        statuses,
    ).fetchall()
    items = []
    for r in rows:
        field_rows = conn.execute(
            "SELECT * FROM expansion WHERE grammar_id=? AND prompt_version NOT LIKE '%:superseded'"
            " ORDER BY id",
            (r["id"],),
        ).fetchall()
        fields = {e["field"]: {"content": e["content"], "confidence": e["confidence"]}
                  for e in field_rows}
        expansion_ids = {e["field"]: e["id"] for e in field_rows}
        examples = [
            {"id": e["id"], "ja": e["ja"], "reading": e["reading"], "zh": e["zh"]}
            for e in conn.execute(
                "SELECT * FROM example WHERE grammar_id=? AND prompt_version NOT LIKE '%:superseded'"
                " ORDER BY id LIMIT 3",
                (r["id"],),
            )
        ]
        items.append(
            {
                "grammar_id": r["id"],
                "title_ja": r["title_ja"],
                "chapter": r["chapter"],
                "content": {**{k: v["content"] for k, v in fields.items()}, "examples": examples},
                "verdict": "",
                "reason": "",
                "field_verdicts": {f: "" for f in EXPANSION_FIELDS + ("examples",)},
                "expansion_ids": expansion_ids,
                "example_ids": [e["id"] for e in examples],
                "fingerprint": fingerprint(fields, examples),
            }
        )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({"items": items}, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    print(f"[export] {len(items)} 条 → {out_path}")
    return len(items)


def do_import(conn, in_path: Path, reviewer: str) -> tuple[int, int]:
    data = json.loads(in_path.read_text(encoding="utf-8"))
    items = data["items"] if isinstance(data, dict) else data
    ok = skipped = 0
    legacy_warned = False
    for it in items:
        verdict = (it.get("verdict") or "").strip()
        if not verdict:
            skipped += 1
            continue
        if verdict not in VERDICT_ORDER:
            print(f"  skip(非法 verdict) grammar_id={it.get('grammar_id')}: {verdict}")
            skipped += 1
            continue
        gid = it["grammar_id"]
        field_verdicts = it.get("field_verdicts") or {}

        # 先取出「当前生效」的扩写和例句,并校验批次是否过期。
        cur_rows = conn.execute(
            "SELECT * FROM expansion WHERE grammar_id=? AND prompt_version NOT LIKE '%:superseded'"
            " ORDER BY id",
            (gid,),
        ).fetchall()
        cur_fields = {e["field"]: {"content": e["content"], "confidence": e["confidence"]}
                      for e in cur_rows}
        cur_ids = {e["field"]: e["id"] for e in cur_rows}
        cur_examples = conn.execute(
            "SELECT * FROM example WHERE grammar_id=? AND prompt_version NOT LIKE '%:superseded'"
            " ORDER BY id LIMIT 3",
            (gid,),
        ).fetchall()
        cur_ex_ids = [x["id"] for x in cur_examples]

        stale: list[str] = []
        fp = it.get("fingerprint")
        if not fp and not it.get("expansion_ids") and not legacy_warned:
            print("  [warn] 该批次没有 fingerprint/expansion_ids(旧格式),"
                  "无法校验是否过期,请重新 export 后再审")
            legacy_warned = True
        if fp and fp != fingerprint(cur_fields, [dict(x) for x in cur_examples]):
            stale.append("内容指纹不匹配")
        exp_ids = it.get("expansion_ids")
        if exp_ids and any(cur_ids.get(f) != i for f, i in exp_ids.items()):
            stale.append("expansion id 不匹配")
        ex_ids = it.get("example_ids")
        if ex_ids is not None and list(ex_ids) != cur_ex_ids:
            stale.append("example id 不匹配")
        if stale:
            print(f"  skip(批次已过期,不套用到新内容) grammar_id={gid}: {' / '.join(stale)}")
            skipped += 1
            continue

        for field in EXPANSION_FIELDS:
            rid = cur_ids.get(field)
            if rid is None:
                continue
            v = field_verdicts.get(field) or verdict
            v = v if v in VERDICT_ORDER else verdict
            conn.execute(
                "INSERT INTO review (target_table, target_id, reviewer, verdict, comment, prompt_version)"
                " VALUES ('expansion',?,?,?,?,?)",
                (rid, reviewer, v, it.get("reason", ""), in_path.name),
            )
        ev = field_verdicts.get("examples") or verdict
        ev = ev if ev in VERDICT_ORDER else verdict
        for ex in cur_examples:
            conn.execute(
                "INSERT INTO review (target_table, target_id, reviewer, verdict, comment, prompt_version)"
                " VALUES ('example',?,?,?,?,?)",
                (ex["id"], reviewer, ev, it.get("reason", ""), in_path.name),
            )
        ok += 1
    conn.commit()
    return ok, skipped


def stats(conn) -> int:
    """多个审查者不一致 → needs_review(expansion 与 example 的 review 都算)。"""
    rows = conn.execute(
        """
        WITH rv AS (
          SELECT e.grammar_id AS gid, r.verdict AS verdict, r.reviewer AS reviewer
            FROM review r JOIN expansion e ON r.target_table='expansion' AND r.target_id=e.id
           WHERE e.prompt_version NOT LIKE '%:superseded'
          UNION ALL
          SELECT x.grammar_id AS gid, r.verdict, r.reviewer
            FROM review r JOIN example x ON r.target_table='example' AND r.target_id=x.id
           WHERE x.prompt_version NOT LIKE '%:superseded'
        )
        SELECT g.id, g.title_ja,
               GROUP_CONCAT(DISTINCT rv.verdict) AS verdicts,
               COUNT(DISTINCT rv.reviewer) AS n_reviewers,
               MAX(CASE rv.verdict WHEN 'wrong' THEN 2 WHEN 'doubtful' THEN 1 ELSE 0 END) AS worst
        FROM grammar_point g JOIN rv ON rv.gid = g.id
        GROUP BY g.id
        """
    ).fetchall()
    conflict = 0
    for r in rows:
        verdicts = set((r["verdicts"] or "").split(","))
        bad = (r["worst"] or 0) > 0
        if r["n_reviewers"] > 1 and len(verdicts) > 1:
            conflict += 1
            conn.execute("UPDATE grammar_point SET status='needs_review' WHERE id=?", (r["id"],))
            print(f"  conflict #{r['id']:>3} {r['title_ja']}: {r['verdicts']} ({r['n_reviewers']} 审查者)")
        elif bad:
            conn.execute("UPDATE grammar_point SET status='needs_review' WHERE id=?", (r["id"],))
    conn.commit()
    print(f"[stats] 多模型分歧 {conflict} 条已置为 needs_review")
    return conflict


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_exp = sub.add_parser("export")
    p_exp.add_argument("--out", default=str(OUT_DIR / "review_batch.json"))
    p_exp.add_argument("--status", default="expanded,needs_review,verified")

    p_imp = sub.add_parser("import")
    p_imp.add_argument("path")
    p_imp.add_argument("--reviewer", default="model:unknown")

    sub.add_parser("stats")

    args = ap.parse_args()
    conn = connect()
    init_db(conn)

    if args.cmd == "export":
        started = now()
        n = export(conn, Path(args.out), tuple(args.status.split(",")))
        log_run(conn, "review_export", "review_batch_v1", started, n, 0, args.out)
        return 0
    if args.cmd == "import":
        started = now()
        ok, skipped = do_import(conn, Path(args.path), args.reviewer)
        log_run(conn, "review_import", "review_batch_v1", started, ok, skipped, args.reviewer)
        print(f"[import] 导入 {ok},跳过 {skipped}")
        print("[import] 接着运行:python src/review_batch.py stats")
        return 0
    stats(conn)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
