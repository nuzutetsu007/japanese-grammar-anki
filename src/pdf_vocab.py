#!/usr/bin/env python3
"""特定技能教材 PDF 的**术语表**:从 pdf2md 产出的 markdown 挖词 → 写进 SQLite 的 term 表。

库(db/grammar.sqlite 的 term 表)是唯一真相来源,md 只是导出物 —— 所以随时可以:
    .venv/bin/python src/pdf_vocab.py ingest data/out/pdf_md/*.ruby.md   # 挖词入库(幂等)
    .venv/bin/python src/pdf_vocab.py list --category 工具・治具 --min-star 4
    .venv/bin/python src/pdf_vocab.py set 圧着工具 --status keep
    .venv/bin/python src/pdf_vocab.py md -o data/out/pdf_md/漢字語_分類.md

挖两类:汉字词(+读音,来自书里的振り仮名)和片假名外来语。

难度是我定的,三档信号合成:
  1. zipf_min   词内最罕用成分的日语 Zipf 频度(wordfreq)。标定:
                N5 基础≈4.4-6.0 / N3≈3.8-4.9 / N2≈2.5-4.6 / 专业≈2.0-3.6 / 超专业<2.6
  2. zipf_whole 整词频度 —— 「熱電対」这种常用字拼的生僻合成词会被 1 低估,用整词拉回
  3. in_wordlist 词形是否在 wordfreq 的日语常用词表(215k 词)里

★ 基础(N5-N4) / ★★ 常见(N3) / ★★★ 教科书(N2) / ★★★★ 难(N1·现场术语) / ★★★★★ 生僻·专业

分类是关键词打分(多字特征 4 分 / 单字领域字 1 分,取最高)。重跑只覆盖
分类/难度/页码这些程序化字段,**不动** status / zh / note(人工与 AI 后填的内容)。
"""

from __future__ import annotations

import argparse
import collections
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common                                        # noqa: E402
from pdf_terms import KATA, merge_pairs, pages_of    # noqa: E402

CANJI = re.compile(r"[\u4e00-\u9fff々]")
EXTRACT_VERSION = "term_v1"
NOTE_VERSION = "term_note_v1"     # zh/note 的版本;改了说明就换版本号

# 类别: 关键词打分(多字词权重高),取分最高者。单字是「领域字」,多字是「强特征」。
CATS: list[tuple[str, tuple[str, ...]]] = [
    ("工具・治具", ("ドライバ", "レンチ", "スパナ", "ペンチ", "ニッパ", "やすり", "鑢", "万力",
                    "バイス", "タップ", "ダイス", "リーマ", "治具", "工具", "器具", "ハンドル",
                    "かんな", "金槌", "ハンマ", "ピンセット", "ドリル", "刃物", "バイト", "クランプ",
                    "チャック", "ボルト", "ナット", "座金", "留め", "定盤", "ジグ", "ヤスリ",
                    "刃", "錐", "鋸", "鉋", "鎚", "鑿")),
    ("測定・検査", ("測定", "計測", "検査", "試験", "ノギス", "マイクロメータ", "ゲージ", "テスタ",
                    "オシロ", "誤差", "精度", "公差", "寸法", "校正", "点検", "判定", "合否",
                    "計器", "目盛", "偏差", "許容", "角度", "計", "測", "量")),
    ("電子部品・電気", ("ダイオード", "トランジスタ", "コンデンサ", "抵抗器", "コイル", "リレー",
                        "コンタクタ", "基板", "集積回路", "端子", "コネクタ", "スイッチ", "ヒューズ",
                        "電線", "ケーブル", "トランス", "配線", "はんだ", "半田", "絶縁", "導体",
                        "電圧", "電流", "抵抗", "磁束", "回路", "接地", "静電気", "電源", "周波数",
                        "充電", "放電", "整流", "半導体", "電池", "蓄電", "電極", "磁気", "電磁",
                        "インダクタ", "配電", "受電", "遮断器", "開閉器", "電動機", "発電", "変圧",
                        "照明", "漏電", "感電", "誘電", "透磁", "圧電", "増幅", "演算",
                        "電", "磁", "回", "絶", "導", "蓄", "充", "放", "灯", "遮", "配")),
    ("機械・装置", ("旋盤", "フライス", "ボール盤", "プレス", "圧縮", "ポンプ", "コンベア", "装置",
                    "機械", "ロボット", "シリンダ", "歯車", "軸受", "ベアリング", "ベルト", "チェーン",
                    "バルブ", "送風", "冷却", "潤滑", "油圧", "空気圧", "タービン", "エンジン",
                    "ブロワ", "クレーン", "搬送", "自動機", "回転", "往復", "盤", "軸", "弁", "機")),
    ("材料", ("金属", "鉄", "銅", "アルミ", "鋼", "樹脂", "プラスチック", "ゴム", "ガラス",
              "セラミック", "エポキシ", "合金", "炭素", "酸素", "材料", "材質", "塗料", "めっき",
              "メッキ", "焼入れ", "腐食", "酸化", "磁性体", "混合物", "材", "油脂")),
    ("加工・工作", ("切削", "研削", "穴あけ", "曲げ", "溶接", "接合", "鍛造", "鋳造", "旋削",
                    "面取り", "バリ", "仕上げ", "組立", "加工", "切断", "絞り", "研磨", "締付",
                    "圧着", "成型", "成形", "打抜", "折曲", "溶断", "熱処理", "塗装", "洗浄",
                    "脱脂", "かしめ", "ねじ", "ネジ", "削", "研", "溶", "圧造")),
    ("製図・図面", ("図面", "製図", "投影", "尺度", "断面", "展開", "記号", "輪郭", "斜視",
                    "正面", "側面", "平面", "立面", "鎖線", "破線", "実線", "点線", "中心線",
                    "図", "線")),
    ("品質管理", ("品質", "管理", "不良", "統計", "ヒストグラム", "特性要因", "散布図", "パレート",
                  "標準偏差", "工程", "改善", "標準化", "保証", "管理図")),
    ("安全衛生", ("安全", "衛生", "災害", "危険", "有害", "保護", "健康", "労働", "事故", "火災",
                  "換気", "粉塵", "毒", "防護", "救急", "熱中症", "爆発", "落下", "墜落", "酸欠",
                  "救護", "雇入", "腰痛", "火気", "絶縁用", "保護具")),
    ("法令・規格", ("法律", "規則", "規格", "基準", "条約", "資格", "免許", "届出", "義務",
                    "責任", "許可", "認定", "条例", "省令", "政令", "労働安全衛生", "電気工事士",
                    "消防法", "法規", "法令", "条項", "違反", "罰則")),
    ("電気工事・配線", ("工事", "配管", "配線", "電線管", "コンセント", "分電盤", "幹線", "引込",
                        "接地工事", "施工")),
    ("単位・量", ("電圧", "電力", "周波数", "温度", "圧力", "長さ", "質量", "密度", "容量",
                  "効率", "抵抗値", "電流値", "磁束密度", "照度", "湿度", "粘度", "硬度",
                  "値", "率", "量", "度")),
]

STOP = {"する", "こと", "もの", "ため", "場合", "以上", "以下", "自体", "一部", "全て"}


def categorize(w: str) -> str:
    """关键词打分: 多字关键词=4 分,单字领域字=1 分,取最高;全 0 → その他。"""
    best, bestscore = "", 0
    for name, keys in CATS:
        sc = sum(4 if len(k) > 1 else 1 for k in keys if k in w)
        if sc > bestscore:
            best, bestscore = name, sc
    return best or "その他"


def stars(zipf_min: float, zipf_whole: float, in_list: bool, nkanji: int) -> int:
    if zipf_min >= 5.0:
        s = 1
    elif zipf_min >= 4.3:
        s = 2
    elif zipf_min >= 3.7:
        s = 3
    elif zipf_min >= 2.7:
        s = 4
    else:
        s = 5
    # 「熱電対」这种常用字拼的生僻合成词:zipf_min 会高估(每个字都常见),
    # 所以再看整词频度 —— 整词在一般日语里也不常见时才算专业术语。
    if not in_list and nkanji >= 3 and zipf_whole < 4.6:
        s = max(s, 4)
    return s


def build(mds: list[str], min_kata: int = 3) -> dict[tuple[str, str], dict]:
    """→ {(词, 读音): {pages, hits, kind}}"""
    out: dict[tuple[str, str], dict] = {}
    for p in mds:
        f = Path(p)
        if not f.exists():
            print(f"跳过(不存在): {f}", file=sys.stderr)
            continue
        for pno, lines in pages_of(f):
            # 换行处被拆开的片假名先拼回去(プロ\nグラマブル → プログラマブル)
            joined = re.sub(r"(?<=[\u30a1-\u30f4\u30f6ー])\n(?=[\u30a1-\u30f4\u30f6ー])", "",
                            "\n".join(lines))
            for ln in lines:
                for term, read in merge_pairs(ln):
                    if len(term) < 2 or not CANJI.search(term) or term in STOP:
                        continue          # 单字多是动词词干(慣/扱/捨),不是词
                    d = out.setdefault((term, read), {"pages": collections.Counter(),
                                                      "hits": 0, "kind": "kanji"})
                    d["pages"][pno] += 1
                    d["hits"] += 1
            for w in KATA.findall(joined):
                if len(w) < min_kata:
                    continue
                d = out.setdefault((w, ""), {"pages": collections.Counter(),
                                             "hits": 0, "kind": "kata"})
                d["pages"][pno] += 1
                d["hits"] += 1
    return out


def dedupe_prefix(rows: dict) -> int:
    """同一词形有多个读音时,若一个是另一个的前缀(送假名被拆掉造成的截断),
    只留更长的那个,出现次数/页码并过去。例: 目盛(めも) 并进 目盛(めもり)。"""
    byterm: dict[str, list] = collections.defaultdict(list)
    for k in rows:
        byterm[k[0]].append(k)
    drop = 0
    for keys in byterm.values():
        if len(keys) < 2:
            continue
        for a in list(keys):
            if a not in rows:
                continue
            for b in keys:
                if b == a or a not in rows or b not in rows:
                    continue
                ra, rb = a[1], b[1]
                if ra and rb and rb != ra and rb.startswith(ra):
                    rows[b]["hits"] += rows[a]["hits"]
                    rows[b]["pages"] += rows[a]["pages"]
                    rows.pop(a)
                    drop += 1
                    break
    return drop


def enrich(rows: dict) -> list[dict]:
    """补上难度/分类/来源页。需要 wordfreq(+MeCab)。"""
    try:
        from wordfreq import iter_wordlist, tokenize, zipf_frequency
    except ImportError:
        sys.exit("需要 wordfreq: uv pip install --python .venv/bin/python "
                 "wordfreq mecab-python3 ipadic")
    WL = set(iter_wordlist("ja", "large"))
    done = []
    for (w, read), d in rows.items():
        try:
            zmin = min(zipf_frequency(t, "ja") for t in tokenize(w, "ja"))
        except Exception:
            zmin = zipf_frequency(w, "ja")
        zwhole = zipf_frequency(w, "ja")
        nk = len(CANJI.findall(w))
        done.append({
            "term": w, "reading": read, "kind": d["kind"],
            "category": categorize(w),
            "difficulty": stars(zmin, zwhole, w in WL, nk),
            "zipf_min": round(zmin, 3), "zipf_whole": round(zwhole, 3),
            "in_wordlist": int(w in WL),
            "hits": d["hits"], "pages": ",".join(str(p) for p in sorted(d["pages"])),
        })
    return done


def ingest(mds: list[str], prune: bool = False) -> int:
    built = build(mds)
    ndup = dedupe_prefix(built)
    rows = enrich(built)
    if ndup:
        print(f"[ingest] 前缀读音去重: 合并 {ndup} 条(如 目盛/めも → 目盛/めもり)")
    src = "+".join(sorted({Path(m).name.replace(".ruby.md", "").replace(".md", "")
                           for m in mds}))[:120]
    conn = common.connect()
    common.init_db(conn)
    ts = common.now()
    if prune:      # 只清掉程序化且没被人动过的行;keep/skip/carded 一律保留
        cur = conn.execute("DELETE FROM term WHERE source=? AND status='new'", (src,))
        print(f"[ingest] --prune: 清掉 {cur.rowcount} 条待挑行")
    new = upd = 0
    for r in rows:
        cur = conn.execute("SELECT id FROM term WHERE term=? AND reading=? AND source=?",
                           (r["term"], r["reading"], src)).fetchone()
        if cur:
            upd += 1
        else:
            new += 1
        # 只覆盖程序化字段;status / zh / note 属于人工与 AI 的内容,重跑不动
        conn.execute(
            "INSERT INTO term (term, reading, kind, category, difficulty, zipf_min,"
            " zipf_whole, in_wordlist, hits, pages, source, created_at, updated_at,"
            " extract_version) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(term, reading, source) DO UPDATE SET"
            " kind=excluded.kind, category=excluded.category, difficulty=excluded.difficulty,"
            " zipf_min=excluded.zipf_min, zipf_whole=excluded.zipf_whole,"
            " in_wordlist=excluded.in_wordlist, hits=excluded.hits, pages=excluded.pages,"
            " updated_at=excluded.updated_at, extract_version=excluded.extract_version",
            (r["term"], r["reading"], r["kind"], r["category"], r["difficulty"],
             r["zipf_min"], r["zipf_whole"], r["in_wordlist"], r["hits"], r["pages"],
             src, ts, ts, EXTRACT_VERSION))
    common.log_run(conn, "pdf_vocab_ingest", EXTRACT_VERSION, ts,
                   new, 0, f"source={src} total={len(rows)} new={new} updated={upd}")
    conn.commit()
    n = conn.execute("SELECT count(*) FROM term").fetchone()[0]
    print(f"[ingest] {src}: 本次 {len(rows)} 词(新 {new} / 更新 {upd}),库里共 {n} 条")
    return 0


def cmd_list(a) -> int:
    conn = common.connect()
    q = ("SELECT term, reading, kind, category, difficulty, hits, status, pages, zh, note,"
         " confidence FROM term WHERE 1=1" if a.wide else
         "SELECT term, reading, kind, category, difficulty, hits, status, pages FROM term WHERE 1=1")
    p: list = []
    if a.category:
        q += " AND category=?"
        p.append(a.category)
    if a.min_star:
        q += " AND difficulty>=?"
        p.append(a.min_star)
    if a.status:
        q += " AND status=?"
        p.append(a.status)
    if a.kind:
        q += " AND kind=?"
        p.append(a.kind)
    if a.search:
        q += " AND (term LIKE ? OR reading LIKE ?)"
        p += [f"%{a.search}%"] * 2
    q += " ORDER BY category, difficulty DESC, hits"
    if a.limit:
        q += f" LIMIT {int(a.limit)}"
    rows = conn.execute(q, p).fetchall()
    for r in rows:
        line = (f"{r['category']:<12}\t{r['term']}\t{r['reading']}\t{'★' * r['difficulty']}"
                f"\t{r['hits']}\t{r['status']}")
        if a.wide:
            conf = r["confidence"] or "—"
            line += f"\t{r['zh'] or '—'}\t{r['note'] or '—'}\t[{conf}]"
        print(line + f"\t{r['pages']}")
    print(f"--- {len(rows)} 条", file=sys.stderr)
    return 0


def cmd_set(a) -> int:
    """人工修正: 改状态,或直接修读音(抽取偶有假名基块被拆导致的截断,如 目盛(めも)→めもり)。"""
    sets, vals = ["status=?", "updated_at=?"], [a.status, common.now()]
    if a.reading:
        sets.append("reading=?")
        vals.append(a.reading)
    vals.append(a.term)
    conn = common.connect()
    cur = conn.execute(f"UPDATE term SET {', '.join(sets)} WHERE term=?", vals)
    conn.commit()
    print(f"[set] {a.term} → status={a.status}"
          + (f" reading={a.reading}" if a.reading else "") + f" (影响 {cur.rowcount} 行)")
    return 0


def cmd_notes(a) -> int:
    """把 TSV(term / zh / note / confidence) 灌进库里。只写人工/AI 后填的字段,
    不碰抽取结果;重跑同一个文件幂等。"""
    import os
    model = os.environ.get("PI_MODEL", "assistant")
    rows = []
    for path in a.tsv:
        f = Path(path)
        if not f.exists():
            print(f"跳过(不存在): {f}", file=sys.stderr)
            continue
        for ln in f.read_text(encoding="utf-8").split("\n"):
            if not ln.strip() or ln.lstrip().startswith("#"):
                continue
            c = ln.split("\t")
            if len(c) < 3:
                print(f"  [warn] 列数不足,忽略: {ln[:40]}", file=sys.stderr)
                continue
            rows.append({"term": c[0].strip(), "zh": c[1].strip(), "note": c[2].strip(),
                         "confidence": (c[3].strip() if len(c) > 3 and c[3].strip()
                                        else "established")})
    conn = common.connect()
    common.init_db(conn)
    ts, miss = common.now(), []
    for r in rows:
        cur = conn.execute(
            "UPDATE term SET zh=?, note=?, confidence=?, note_model=?, note_version=?,"
            " updated_at=? WHERE term=?", (r["zh"], r["note"], r["confidence"], model,
                                           NOTE_VERSION, ts, r["term"]))
        if cur.rowcount == 0:
            miss.append(r["term"])
    common.log_run(conn, "term_notes", NOTE_VERSION, ts, len(rows) - len(miss), len(miss),
                   f"model={model} files={len(a.tsv)}")
    conn.commit()
    print(f"[notes] {len(rows) - len(miss)}/{len(rows)} 条写入(model={model}, {NOTE_VERSION})")
    if miss:
        print(f"  库里没有(检查词形): {', '.join(miss)}")
    return 0


def cmd_md(a) -> int:
    conn = common.connect()
    rows = conn.execute(
        "SELECT * FROM term WHERE difficulty>=? ORDER BY category, difficulty DESC, hits",
        (a.min_star,)).fetchall()
    bycat: dict[str, list] = collections.defaultdict(list)
    for r in rows:
        bycat[r["category"]].append(r)
    order = [c for c, _ in CATS] + ["その他"]
    L = ["# 术语表(term 表的导出物)", "",
         f"来源 PDF: {', '.join(sorted({r['source'] for r in rows}))}",
         f"共 {len(rows)} 词(★{a.min_star} 以上)", ""]
    L += ["★ 基础(N5-N4) / ★★ 常见(N3) / ★★★ 教科书(N2) / ★★★★ 难(N1·现场术语) / ★★★★★ 生僻·专业",
          "", "| 类别 | 词数 | ★4-5 |", "|---|---:|---:|"]
    for c in order:
        rs = bycat.get(c)
        if rs:
            L.append(f"| {c} | {len(rs)} | {sum(1 for r in rs if r['difficulty'] >= 4)} |")
    L.append("")
    for c in order:
        rs = bycat.get(c)
        if not rs:
            continue
        L += [f"## {c}（{len(rs)} 词）", "", "| 词 | 读音 | 種類 | 難 | 次 | 页 |",
              "|---|---|:--:|:--:|---:|---|"]
        for r in rs:
            pages = r["pages"].split(",")
            ps = ", ".join(pages[:10]) + ("…" if len(pages) > 10 else "")
            L.append(f"| {r['term']} | {r['reading']} | {r['kind']} | "
                     f"{'★' * r['difficulty']} | {r['hits']} | {ps} |")
        L.append("")
    text = "\n".join(L) + "\n"
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text(text, encoding="utf-8")
        print(f"→ {a.out}")
    else:
        print(text)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="术语表: 挖词 → term 表 → 导出")
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("ingest", help="从 pdf2md 的 markdown 挖词写库(幂等)")
    p.add_argument("md", nargs="+")
    p.add_argument("--prune", action="store_true",
                   help="先删掉本来源下 status='new' 的行(人工改过的保留),再重新写入")
    p.set_defaults(func=ingest_dispatch)

    p = sub.add_parser("list", help="查库")
    p.add_argument("--category", default="")
    p.add_argument("--min-star", type=int, default=0, dest="min_star")
    p.add_argument("--status", default="")
    p.add_argument("--kind", default="", choices=["", "kanji", "kata"])
    p.add_argument("--search", default="")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--wide", action="store_true", help="带上 zh / note / confidence")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("set", help="改状态: new/keep/skip/carded")
    p.add_argument("term")
    p.add_argument("--status", default="keep",
                   choices=["new", "keep", "skip", "carded"])
    p.add_argument("--reading", default="", help="顺便修读音(人工纠正抽取错误)")
    p.set_defaults(func=cmd_set)

    p = sub.add_parser("notes", help="把 TSV(term/zh/note/confidence) 灌进库")
    p.add_argument("tsv", nargs="+")
    p.set_defaults(func=cmd_notes)

    p = sub.add_parser("md", help="导出 markdown 报告")
    p.add_argument("-o", "--out", default="")
    p.add_argument("--min-star", type=int, default=1, dest="min_star")
    p.set_defaults(func=cmd_md)

    a = ap.parse_args()
    if not a.cmd:
        ap.print_help()
        return 1
    return a.func(a)


def ingest_dispatch(a) -> int:
    return ingest(a.md, prune=a.prune)


if __name__ == "__main__":
    raise SystemExit(main())
