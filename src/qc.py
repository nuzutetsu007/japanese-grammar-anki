#!/usr/bin/env python3
"""阶段 3.2:程序化质检(qc)—— 不花 API 的硬规则检查。

抽查发现小模型最容易出的错是「整句假名注音」和「乱码/简繁混用」,
这些用规则就能抓,而且比让模型自己检查可靠得多。

抓到的每个问题都会写成一条 `review` 记录(reviewer='qc:rule'),
这样现有的 `repair.py` 流程会自动把它当成「审校意见」去修。

用法:
    python3 src/qc.py            # 检查全部,写 review
    python3 src/qc.py --dry      # 只报告,不写库
    python3 src/qc.py --chapter N2文法
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import connect, init_db, log_run, now  # noqa: E402

PROMPT_VERSION = "qc_v1"

# 中文简体字里「日文完全不这么写」的那些(日文用 問/門/見/説/語/読/…)
# 注意:来 国 写 与 各 别 体 内 党 宝 当 台 号 参 双 学 医 制 强 等**日文也这么写**,
# 不在名单里,否则会误报。
CN_ONLY = set(
    "问门见说语读书车东长关开无记试让认识课员图买卖难这时话听为变对头业务动"
    "应该实专进过还们个义举乐习乡亚产亲仅从众优传伟价儿军农决况净减则刚创"
    "剂势华协单卫历压厌县发团园围圆场坏块坚备复奋奖妇娱宁审导尔层岁币师帐"
    "带归录彻忆废异张严丧丰临丽乌乔乱争于亏云亩亿仑仓战药帮岁运动"
)

KANA = re.compile(r"[ぁ-ゖァ-ヺー]")
KANJI = re.compile(r"[一-鿿々]")
LATIN = re.compile(r"[A-Za-z]")
# 一个片假名夹在汉字/平假名中间(「教室メ」「ほんネんど」这种)
STRAY_KATA = re.compile(r"(?:[ぁ-ゖ一-鿿])[ァ-ヺ](?:[ぁ-ゖ一-鿿])")


def kana_of(s: str) -> str:
    """把句子里的假名按顺序抽出来(片假名当平假名处理)。"""
    out = []
    for ch in s:
        if KANA.match(ch):
            out.append(chr(ord(ch) - 0x60) if "ァ" <= ch <= "ヺ" else ch)
    return "".join(out)


def normalize_kana(s: str) -> str:
    s = s.replace("・", "").replace(" ", "").replace("　", "")
    return "".join(chr(ord(c) - 0x60) if "ァ" <= c <= "ヺ" else c for c in s)


def is_subsequence(needle: str, hay: str) -> bool:
    """needle 是否按顺序出现在 hay 里(允许 hay 多出别的字符)。"""
    it = iter(hay)
    return all(ch in it for ch in needle)


def check_example(ex: dict) -> list[str]:
    """检查一条例句,返回问题列表。"""
    issues: list[str] = []
    ja, rd, conn = ex["ja"] or "", ex["reading"] or "", ex["conn"] or ""

    # 1) 假名注音
    if not rd:
        issues.append("假名注音为空,请补上整句平假名注音")
    else:
        if KANJI.search(rd):
            bad = "".join(sorted(set(KANJI.findall(rd))))
            issues.append(f"假名注音里还有汉字「{bad}」,注音必须全是平假名")
        if LATIN.search(rd):
            bad = "".join(sorted(set(LATIN.findall(rd))))
            issues.append(f"假名注音里混进了拉丁字母「{bad}」")
        # 句子里的假名序列必须按顺序出现在注音里(漏字/多字/乱码都能抓到)
        want = kana_of(ja)
        if want and not is_subsequence(want, normalize_kana(rd)):
            issues.append(f"假名注音与句子对不上(句子的假名序列「{want}」没有按顺序出现在注音里),"
                          "请按汉字逐字重新注音")
        elif not (0.9 <= len(rd) / max(len(ja), 1) <= 2.6):
            issues.append(f"假名注音长度异常(句子 {len(ja)} 字 / 注音 {len(rd)} 字),请核对是否漏注或重复")

    # 2) 乱码:单个片假名夹在汉字/平假名之间
    for m in STRAY_KATA.finditer(ja):
        issues.append(f"例句里有可疑的单独片假名字符「{m.group(0)}」,像是乱码或多余字符,请删掉或改对")
    for m in STRAY_KATA.finditer(rd):
        issues.append(f"注音里有可疑的单独片假名字符「{m.group(0)}」")

    # 3) 简繁混用
    bad = sorted({c for c in ja if c in CN_ONLY})
    if bad:
        issues.append(f"例句里用了中文简体字「{''.join(bad)}」,日语里应该写成对应的日文汉字")

    # 4) 接续标签与句子对不上(粗查:标 V-た 但文法点前面不是 た/だ)
    m = re.fullmatch(r"\s*(V-た|V-ない|V-る|V-て|N\+?の?|い形|な形\+?な?)\s*", conn)
    if m:
        label = m.group(1)
        if label == "V-た" and not re.search(r"[ただ]が早いか|[ただ]そばから|[ただ]が最後", ja):
            pass  # 具体判定交给 verify,这里不硬判
    if not conn:
        issues.append("例句缺少 conn(接续)标签")
    return issues


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="只报告,不写 review")
    ap.add_argument("--chapter", default="")
    args = ap.parse_args()

    started = now()
    conn = connect()
    init_db(conn)

    sql = ("SELECT e.*, g.title_ja, g.chapter FROM example e"
           " JOIN grammar_point g ON g.id = e.grammar_id"
           " WHERE e.prompt_version NOT LIKE '%:superseded'")
    params: list = []
    if args.chapter:
        sql += " AND g.chapter LIKE ?"
        params.append(f"%{args.chapter}%")
    rows = [dict(r) for r in conn.execute(sql, params)]

    bad_items: dict[int, list[str]] = {}
    n_issues = 0
    seen_order: dict[int, int] = {}
    for ex in rows:
        gid = ex["grammar_id"]
        seen_order[gid] = seen_order.get(gid, 0) + 1
        issues = check_example(ex)
        if not issues:
            continue
        n_issues += len(issues)
        tag = f"例句{seen_order[gid]}({(ex['ja'] or '')[:14]}…)"
        bad_items.setdefault(gid, []).extend(f"{tag}: {s}" for s in issues)

    print(f"[qc] 检查 {len(rows)} 条例句,{len(bad_items)} 个文法点有 {n_issues} 处问题")
    for gid, issues in list(bad_items.items())[:12]:
        print(f"  #{gid} {conn.execute('select title_ja from grammar_point where id=?', (gid,)).fetchone()[0]}")
        for s in issues[:3]:
            print(f"      - {s}")

    if not args.dry and bad_items:
        for gid in bad_items:
            conn.execute(
                "INSERT INTO review (target_table, target_id, reviewer, verdict, comment,"
                " prompt_version) VALUES ('expansion', ?, 'qc:rule', 'doubtful', ?, ?)",
                (conn.execute("SELECT id FROM expansion WHERE grammar_id=? AND prompt_version"
                              " NOT LIKE '%:superseded' LIMIT 1", (gid,)).fetchone()[0],
                 "【程序化质检】" + " / ".join(bad_items[gid][:6]), PROMPT_VERSION),
            )
            conn.execute("UPDATE grammar_point SET status='needs_review' WHERE id=?", (gid,))
        conn.commit()

    log_run(conn, "qc", PROMPT_VERSION, started, len(bad_items), 0,
            f"{len(rows)} 条例句 / {n_issues} 处问题")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
