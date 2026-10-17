#!/usr/bin/env python3
"""term 表:噪声清理 + 重要度打分/分档(都只动程序化字段,可重跑)。

清理只做**人工过目过的白名单**,分两类:
  * DROP:   重复写形 / 胶着片假名 / 片段词 → status='skip'。
            下一次出卡时带「用語::削除候補」标签再发一次(方便在 Anki 里搜索删卡),
            然后 skip_sent=1,以后不再发。
  * RENAME: 错词形/错读音 → 旧 "term:reading" 存进 guid_key,
            重出卡时 GUID 不变 → Anki 导入是原地更新,学习进度不丢。

打分(全部用 term 表既有字段,不引新数据):
  hits 教材出现次数 + difficulty ★ + 片假名外来语 + 是否在常用词表 + 领域加权,
  基础词降权。按分数名次切三档:1=必須(top 300)/ 2=重要(300-900)/ 3=補完(其余)。

用法:
    .venv/bin/python src/term_triage.py --dry-run   # 只看计划与分档
    .venv/bin/python src/term_triage.py             # 写库(幂等)
"""

from __future__ import annotations

import argparse
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import connect, init_db, log_run, now   # noqa: E402

VERSION = "term_triage_v1"
CUT_A, CUT_B = 300, 900          # 名次切线:≤300 → 档1;≤900 → 档2;其余档3

# ---------------------------------------------------------------------------
# 剔除白名单(term, reading, 理由)。都是人过目过的:
# 重复写形留书里出现更多的那个;胶着/片段词没有补救价值。
# ---------------------------------------------------------------------------
DROPS: list[tuple[str, str, str]] = [
    # 重复写形
    ("コンピューター", "", "与「コンピュータ」重复(16:1)"),
    ("ニッパー", "", "与「ニッパ」重复(书里 1:4)"),
    ("モーター", "", "与「モータ」重复(1:6)"),
    ("不足", "ぶそく", "读音变体(正:ふそく)"),
    ("熱起電力", "ねっきでんりょく", "读音错(正:ねつきでんりょく)"),
    ("仕上", "しあ", "送り仮名截断,留「仕上げ」"),
    ("回路版", "かいろばん", "错字(正:回路板)"),
    ("組立て", "くみたて", "与「組立」重复(2:10)"),
    ("射出成型", "しゃしゅつせいけい", "与「射出成形」重复(1:10)"),
    ("成型", "せいけい", "与「成形」重复(2:23)"),
    ("目盛り", "めもり", "与「目盛」重复(15:18)"),
    # 读音被截断的小复合词(成分词都在,可推导,价值低)
    ("機器組立", "ききくみた", "读音截断(正:ききくみたて)"),
    ("変圧器組立", "へんあつきくみた", "读音截断"),
    ("制御盤組立", "せいぎょばんくみた", "读音截断"),
    # 胶着片假名 / 抽取碎片
    ("チェックシートデータ", "", "胶着:チェックシート+データ"),
    ("ヒストグラムデータ", "", "胶着:ヒストグラム+データ"),
    ("グラフデータ", "", "胶着:グラフ+データ"),
    ("パーツパーミリオン", "", "胶着:parts per million"),
    ("リチウムイオンモバイルバッテリー", "", "胶着(保留「モバイルバッテリー」)"),
    # 片段/短语(不是术语)
    ("組み", "くみ", "复合词片段(組立/組み合わせ)"),
    ("切り", "きり", "复合词片段"),
    ("他測定器", "たそくていき", "「その他の測定器」碎片,读音也是拼的"),
    ("品質管理上重要な", "ひんしつかんりじょうじゅうような", "形容词短语"),
    ("変圧器電気機器", "へんあつきでんききき", "表格行胶着:変圧器+電気機器"),
]

# ---------------------------------------------------------------------------
# 改名/改读音白名单。guid_key 记录旧键 → 重出卡时 GUID 不变,Anki 原地更新。
# zh/note 给 None 就保持原值。
# ---------------------------------------------------------------------------
RENAMES: list[dict] = [
    {"term": "動作", "reading": "せいていじかん",
     "new_term": "整定時間", "new_reading": "せいていじかん",
     "zh": "整定时间",
     "note": "定时器上设定的延迟时间 t:输入信号变化后,延迟该设定时间才动作。"},
    {"term": "見積", "reading": "みつ",
     "new_term": "見積", "new_reading": "みつもり",
     "zh": None, "note": None},     # 只修读音(みつ → みつもり)
]

# ---------------------------------------------------------------------------
# 重要度打分
# ---------------------------------------------------------------------------
CAT_W = {
    "電子部品・電気": 1.5, "測定・検査": 1.5, "工具・治具": 1.5,
    "製図・図面": 1.5, "電気工事・配線": 1.5,
    "機械・装置": 1.0, "加工・工作": 1.0, "材料": 1.0,
    "品質管理": 1.0, "安全衛生": 1.0, "法令・規格": 1.0,
    "単位・量": 0.5, "その他": 0.0,
}
ELIGIBLE_STATUS = ("carded", "keep")
OK_CONF = ("established", "common_claim")


def score(r: dict) -> float:
    s = (3.0 * math.log2(r["hits"] + 1)
         + 2.0 * r["difficulty"]
         + (2.0 if r["kind"] == "kata" else 0.0)
         + (1.5 if not r["in_wordlist"] else 0.0)
         + CAT_W.get(r["category"], 0.0))
    if r["difficulty"] <= 2 and r["in_wordlist"] and r["kind"] == "kanji":
        s -= 4.0                       # 基础词(上下/画面/取る)别来凑热闹
    return s


def apply_fixes(conn, dry: bool) -> tuple[int, int, list[str]]:
    log: list[str] = []
    n_drop = n_ren = 0
    for term, reading, reason in DROPS:
        r = conn.execute("SELECT id, status FROM term WHERE term=? AND reading=?",
                         (term, reading)).fetchone()
        if not r:
            log.append(f"  ! 未找到(跳过): {term}({reading})")
            continue
        if r[1] == "skip":
            continue
        if not dry:
            conn.execute("UPDATE term SET status='skip', updated_at=? WHERE id=?",
                         (now(), r[0]))
        n_drop += 1
        log.append(f"  drop  {term}({reading})  — {reason}")
    for f in RENAMES:
        r = conn.execute("SELECT id, guid_key FROM term WHERE term=? AND reading=?",
                         (f["term"], f["reading"])).fetchone()
        if not r:
            log.append(f"  ! 未找到(跳过): {f['term']}({f['reading']})")
            continue
        if r[1] is None:               # 已经是目标状态就跳过(幂等)
            dup = conn.execute("SELECT id FROM term WHERE term=? AND reading=? AND id<>?",
                               (f["new_term"], f["new_reading"], r[0])).fetchone()
            if dup:
                sys.exit(f"改名目标已存在,会撞 UNIQUE: {f['new_term']}({f['new_reading']})")
            gk = f"{f['term']}:{f['reading']}"
            if not dry:
                conn.execute(
                    "UPDATE term SET term=?, reading=?, guid_key=?,"
                    " zh=COALESCE(?, zh), note=COALESCE(?, note), updated_at=?"
                    " WHERE id=?",
                    (f["new_term"], f["new_reading"], gk, f["zh"], f["note"], now(), r[0]))
            n_ren += 1
            log.append(f"  rename {f['term']}({f['reading']}) → "
                       f"{f['new_term']}({f['new_reading']})  guid_key={gk}")
    return n_drop, n_ren, log


def run(dry: bool) -> int:
    conn = connect()
    init_db(conn)
    started = now()

    n_drop, n_ren, fixlog = apply_fixes(conn, dry)
    print(f"[triage] 清理: drop {n_drop} / rename {n_ren}")
    for line in fixlog:
        print(line)

    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM term WHERE status IN (?,?) AND confidence IN (?,?)",
        (*ELIGIBLE_STATUS, *OK_CONF))]
    for r in rows:
        r["score"] = score(r)
    rows.sort(key=lambda r: (-r["score"], -r["hits"], -r["difficulty"], r["term"]))
    for i, r in enumerate(rows):
        r["tier"] = "1" if i < CUT_A else "2" if i < CUT_B else "3"

    if not dry:
        for r in rows:
            conn.execute("UPDATE term SET priority=?, tier=?, updated_at=? WHERE id=?",
                         (round(r["score"], 3), r["tier"], now(), r["id"]))
        conn.commit()

    by_tier = {t: [r for r in rows if r["tier"] == t] for t in ("1", "2", "3")}
    print(f"[triage] 可出卡 {len(rows)} 条 → 必須 {len(by_tier['1'])} / "
          f"重要 {len(by_tier['2'])} / 補完 {len(by_tier['3'])}"
          + ("  (dry-run,未写库)" if dry else ""))
    cat1 = {}
    for r in by_tier["1"]:
        cat1[r["category"]] = cat1.get(r["category"], 0) + 1
    print("    档1 类别分布: " + " / ".join(f"{k} {v}" for k, v in
                                          sorted(cat1.items(), key=lambda x: -x[1])))
    for lo, hi in ((CUT_A - 3, CUT_A + 2), (CUT_B - 3, CUT_B + 2)):
        print(f"    切线 {lo}~{hi}: " + " | ".join(
            f"{i + 1}.{rows[i]['term']}(★{rows[i]['difficulty']},h{rows[i]['hits']})"
            for i in range(lo, min(hi, len(rows)))))
    log_run(conn, "term_triage", VERSION, started, len(rows), 0,
            f"drop={n_drop} rename={n_ren} cut={CUT_A}/{CUT_B}"
            + (" (dry)" if dry else ""))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="term 噪声清理 + 重要度分档")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    return run(a.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
