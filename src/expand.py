#!/usr/bin/env python3
"""阶段 2:扩写(expand)。

对每个 `status='new'` 的文法点调用一次模型,要求只输出 JSON,
写入 expansion(按字段拆开)和 example(3 条),然后把 status 改成 'expanded'。

幂等 / 可断点续跑:默认只处理 status='new';失败的保持 'new',重跑即可。
旧版本内容不会被覆盖 —— 想重跑请用 --status expanded --new-version,
旧行保留在 expansion 表里(按 prompt_version 区分)。新内容成功入库时,
旧行才被标 :superseded;请求失败则旧内容继续生效。

用法:
    python3 src/expand.py --limit 3                 # 试跑 3 条
    python3 src/expand.py --chapter N2文法          # 只跑某个章节
    python3 src/expand.py                           # 全量
    python3 src/expand.py --ids 1,5,9               # 指定
    python3 src/expand.py --workers 8
    python3 src/expand.py --model openai/gpt-oss-120b   # 指定单个模型(临时不受容灾链保护)
    python3 src/expand.py --status expanded --new-version   # 换提示词后全量重跑
"""
from __future__ import annotations

import argparse
import json
import os
import re
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

PROMPT_VERSION = "expand_v4"
MAX_EXAMPLES = int(os.environ.get("GRAMMAR_MAX_EXAMPLES", "8"))
RAW_DIR = OUT_DIR / "raw"


def build_user_prompt(row) -> str:
    extra = []
    if row["reading_romaji"] if "reading_romaji" in row.keys() else "":
        extra.append(f"来源页给的罗马字读音:{row['reading_romaji']}(仅参考)")
    return (
        "文法点:「{title}」\n"
        "所在位置:{chapter}\n"
        "来源索引:{page}\n"
        "{extra}\n"
        "请输出 expand 要求的 JSON。"
    ).format(
        title=row["title_ja"],
        chapter=row["chapter"],
        page=row["page_ref"],
        extra="\n".join(extra),
    )


def valid(data) -> bool:
    return any(f in data for f in EXPANSION_FIELDS) and isinstance(data.get("examples"), list)


def expand_one(row, system_prompt: str, model: str, gloss: str = "") -> dict:
    user = build_user_prompt(row)
    if gloss:
        user = user.replace("请输出", f"来源页英文释义(仅参考,不要直译):{gloss}\n\n请输出")
    data = chat_json(
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user},
        ],
        model=model,
        validate=valid,
        max_tokens=2600,
        expect_out=1800,
    )
    data["_grammar_id"] = row["id"]
    (RAW_DIR / f"{row['id']:03d}.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return data


def norm_field(value):
    """把字段内容统一成 (content|None, confidence)。"""
    if value is None:
        return None, "uncertain"
    if isinstance(value, str):
        return (value.strip() or None), "uncertain"
    content = value.get("content")
    conf = value.get("confidence") or "uncertain"
    if conf not in ("established", "common_claim", "mnemonic_only", "uncertain"):
        conf = "uncertain"
    if isinstance(content, str):
        content = content.strip() or None
    return content, conf


def norm_reading(s: str) -> str:
    """假名注音去掉空格/全角空格。"""
    return re.sub(r"[\s　]+", "", (s or "").replace("・", ""))


def save(conn, row, data, prompt_version: str) -> tuple[int, int]:
    gid = row["id"]
    model = data.get("_model") or "unknown"

    # 先把要写的新内容解析出来。一个字段/例句都解析不出就直接失败:
    # 否则旧内容被标 superseded、新内容又是空的 = 这条文法点被清空
    # (踩过一次:#260「上で」整条消失,还被 verify 盖了 verified)。
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
        raise RuntimeError("扩写结果里没有任何可用字段/例句,保留旧内容不动")

    # 新内容确认可用后,才在同一个事务里作废旧内容 + 写新内容。
    # 若模型请求失败,根本不会走到这里,旧内容继续保持生效(可断点续跑)。
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

    # 文法点自身的假名读法:只在库里的 reading 还是空的时候写回去
    tr, tr_conf = norm_field(data.get("title_reading"))
    if tr and tr_conf != "uncertain":
        conn.execute(
            "UPDATE grammar_point SET reading=? WHERE id=? AND (reading IS NULL OR reading='')",
            (tr, gid),
        )

    for field, content, conf in fields:
        conn.execute(
            "INSERT INTO expansion (grammar_id, field, content, confidence, model, prompt_version)"
            " VALUES (?,?,?,?,?,?)",
            (gid, field, content, conf, model, prompt_version),
        )
    for ja, reading, zh, conn_label in examples:
        conn.execute(
            "INSERT INTO example (grammar_id, ja, reading, zh, conn, origin, verified, model,"
            " prompt_version) VALUES (?,?,?,?,?, 'ai', 0, ?, ?)",
            (gid, ja, reading, zh, conn_label, model, prompt_version),
        )
    conn.execute("UPDATE grammar_point SET status='expanded' WHERE id=?", (gid,))
    conn.commit()
    return len(fields), len(examples)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--ids", default="")
    ap.add_argument("--status", default="new", help="要处理的 status(可逗号分隔多个)")
    ap.add_argument("--chapter", default="", help="只处理 chapter 包含该字符串的行")
    ap.add_argument("--model", default="",
                    help="留空=用容灾链(GRAMMAR_MODELS),也可写单模型名")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--new-version", action="store_true",
                    help="配合 --status expanded:用带时间戳的 prompt_version 重跑"
                         "(旧内容会在新内容成功入库时被标 :superseded)")
    args = ap.parse_args()

    prompt_version = PROMPT_VERSION
    if args.new_version:
        prompt_version = f"{PROMPT_VERSION}_run{now().replace(' ', 'T')}"
    system_prompt = read_prompt(PROMPT_VERSION)

    conn = connect()
    init_db(conn)
    RAW_DIR.mkdir(parents=True, exist_ok=True)

    sts = [x.strip() for x in args.status.split(",") if x.strip()]
    sql = "SELECT * FROM grammar_point WHERE status IN ({})".format(",".join("?" * len(sts)))
    params: list = list(sts)
    if args.chapter:
        sql += " AND chapter LIKE ?"
        params.append(f"%{args.chapter}%")
    if args.ids:
        ids = [int(x) for x in args.ids.split(",") if x.strip()]
        sql += f" AND id IN ({','.join('?' * len(ids))})"
        params += ids
    sql += " ORDER BY book_order"
    if args.limit:
        sql += f" LIMIT {args.limit}"
    rows = conn.execute(sql, params).fetchall()

    if not rows:
        print(f"[expand] 没有 status={args.status} 的行,无需处理")
        return 0

    started = now()
    glosses = {r["grammar_id"]: (r["source_gloss"] or "") for r in conn.execute(
        "SELECT grammar_id, source_gloss FROM book_fact")}
    print(f"[expand] {len(rows)} 条待处理,model={args.model or '<容灾链>'},workers={args.workers}")
    ok = failed = 0
    failures: list[str] = []

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(expand_one, r, system_prompt, args.model,
                               glosses.get(r["id"], "")): r for r in rows}
        for fut in as_completed(futures):
            row = futures[fut]
            try:
                data = fut.result()
                nf, ne = save(conn, row, data, prompt_version)
                ok += 1
                print(f"  ok  #{row['id']:>3} {row['title_ja']}  fields={nf} examples={ne}")
            except Exception as e:  # noqa: BLE001
                failed += 1
                failures.append(f"#{row['id']} {row['title_ja']}: {e}")
                print(f"  ERR #{row['id']:>3} {row['title_ja']}: {e}", file=sys.stderr)

    log_run(conn, "expand", prompt_version, started, ok, failed,
            "; ".join(failures)[:900])
    print(f"[expand] 完成 {ok},失败 {failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
