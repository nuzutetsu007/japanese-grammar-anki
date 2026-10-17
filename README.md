# grammar-anki — 日语文法书 → SQLite → Anki

把《日本語総まとめ N1 文法》目录照片 OCR 成结构化文法点,用 AI 逐条扩写(接续 / 意思 / 汉字写法 / 由来 / 用法 / 对比 / 例句),
经自检与人工审核后导出成 Anki 牌组。**SQLite 是唯一真相来源**,Anki 只是导出物。

## 快速开始

```bash
cp .env.example .env && chmod 600 .env  # 填模型 API key(至少一个)
python3 src/init_db.py                  # 建库(幂等)
python3 src/ingest.py --list            # 看目录清单,人工确认一次
python3 src/ingest.py                   # 写入 grammar_point
python3 src/expand.py --limit 3         # 试跑 3 条看质量
python3 src/expand.py --workers 6        # 全量扩写(带模型容灾)
python3 src/verify.py --workers 6        # 自检(挑错)
python3 src/repair.py --workers 4        # 只修被标 doubtful/wrong 的行
python3 src/verify.py --workers 4        # 重审修复过的行
python3 src/review_queue.py --show 5     # 人工只看被标记的
python3 src/eval_golden.py               # 对比黄金样本
python3 src/build_cards.py               # 生成卡片
python3 src/sample_check.py -n 10         # 随机抽 10 条输出 md 给人看
.venv/bin/python src/export_anki.py      # 打包 .apkg(需要 genanki)
```

导出的牌组:`data/out/日本語文法.apkg`,两个牌组 **`日本語文法::N1`** / **`日本語文法::N2`**
(note 上带 `文法` / `文法::N1` / `文法::N1::第1週1日目` 标签树)。

## 目录

```
input/toc/toc_ocr.json   目录照片的 OCR 结果(人工目视核对过)
db/grammar.sqlite        唯一真相来源
prompts/                 每个提示词一个文件,带版本号
  ingest_toc_v1.md       目录 OCR 校对
  expand_v1.md           扩写(接续/意思/语源/例句)
  verify_v1.md           挑错(日语母语审校者角色)
  repair_v1.md           带「审校者意见」的定向修复
src/
  common.py              路径 / DB / run_log / HTTP 调模型(配额调度 + 容灾)
  init_db.py             阶段 0 建表
  ingest.py              阶段 1 导入目录
  expand.py              阶段 2 扩写
  verify.py              阶段 3 自检
  repair.py              阶段 3.5 定向修复(只修被标记的行)
  review_batch.py        阶段 4 外部模型校准(export / import / stats)
  review_queue.py        阶段 5 人工审核队列
  build_cards.py         阶段 6 生成卡片
  sample_check.py        阶段 8 随机抽检(输出 md 给人看)
  export_anki.py         阶段 7 导出 .apkg
  eval_golden.py         黄金样本对比
eval/golden.json         人工确认过的标准答案样本
data/out/                中间产物(raw 模型原始输出、cards.json、.apkg、日志)
```

## 模型怎么调用的

**直接走 OpenAI 兼容的 HTTP API**(`httpx`),不 shell 出 `pi` 子进程 —— 每条文法点
起一个进程开销太大,HTTP 直调几百条几十秒就能发完。

* Provider 定义在 `src/common.py` 的 `PROVIDERS`:`groq` / `openrouter` / `google`,
  各自只差 base_url 和 key 环境变量。
* API key 从项目根目录的 `.env` 读取(`common.load_env()`,不依赖 python-dotenv):
  `GROQ_API_KEY` / `OPENROUTER_API_KEY` / `GOOGLE_API_KEY`。`.env` 权限建议保持 `600`。
* 依赖 `httpx`(系统 `python3` 已装)。

### 模型池 + 配额感知调度

`common.chat()` 不是简单的重试链,而是一套**令牌桶 + 每日账本**调度:

* 每个 `provider:model` 单独算最近 60 秒滑窗的 RPM / TPM / OTPM,发请求前先占额度,
  拿到真实 `usage` 后回填,所以基本不会真的吃到 429。
* 每日用量落盘到 `data/out/usage/YYYY-MM-DD.json`,跨进程/重启累计,到 RPD / TPD 上限
  就自动跳过该模型。
* 并发线程各自挑「剩余配额比例最高」的模型 → 天然负载均衡。
* 真遇上 429/5xx 就冷却该模型并换下一个;配额冷却默认 60 秒,普通错误 20 秒,上限 300 秒。
* 声明值超过 OTPM 的模型(例如 Groq 对长输出)会直接被跳过,不浪费请求。

默认模型池(`GRAMMAR_MODELS`),覆盖三种 provider:

```
google:gemini-flash-lite-latest            大输出主力(10K TPM 级、按实际用量扣)
google:gemini-3.1-flash-lite
openrouter:nvidia/nemotron-3-ultra-550b-a55b:free
openrouter:nvidia/nemotron-3-super-120b-a12b:free
openrouter:qwen/qwen3.8-27b:free
openrouter:stealth/space-bunny-alpha
groq:openai/gpt-oss-120b                    小输出(verify 之类,按声明 max_tokens 扣 TPM)
groq:qwen/qwen3.8-27b
groq:openai/gpt-oss-20b
```

覆盖方式:

```bash
# 换整条链
GRAMMAR_MODELS='google:gemini-flash-lite-latest,openrouter:qwen/qwen3.8-27b:free' \
    python3 src/expand.py --workers 6

# 只跑一个模型
python3 src/repair.py --model google:gemini-3.1-flash-lite

# 调冷却时间(秒)
GRAMMAR_QUOTA_COOLDOWN=300 python3 src/expand.py

# 调每日用量上限(示例:覆盖 groq 的 rpd/tpd)
GRAMMAR_GROQ_RPD=500 GRAMMAR_GROQ_TPD=100000 python3 src/verify.py
```

## 卡片样式(含 dark mode)

样式集中在 `src/export_anki.py` 的 `CSS` 常量里,三种卡片类型共用。

**暗色模式的做法**(查了官方文档 + AnkiDroid wiki + 2025 年论坛 issue):

| 客户端 | 选择器 |
|---|---|
| Anki Desktop / AnkiMobile | `.card.nightMode`、`.nightMode .card` |
| AnkiDroid | `.card.night_mode`、`.night_mode .card`(下划线) |
| 跟随系统的客户端 | `@media (prefers-color-scheme: dark)` |

注意:**不能只靠 `prefers-color-scheme`** —— Anki Desktop(Qt webview)即使切到暗色
也只会报 `light`,这是已知问题;所以必须同时支持 `nightMode` / `night_mode` 类。

具体做法是 CSS 变量:

```css
.card { --bg: #fbfbfd; --fg: #1c1c1e; --accent: #0b57d0; ... }
.card.nightMode, .nightMode .card, .card.night_mode, .night_mode .card {
  --bg: #1e1e22; --fg: #e8e8ec; --accent: #8ab4f8; ...
}
@media (prefers-color-scheme: dark) { .card { /* 同一套暗色变量 */ } }
```

其他可读性上的取舍:

* 不用纯黑/纯白(`#1e1e22` / `#fbfbfd`),减少长时间阅读的眩光。
* 正文 `19px`、例句 `22px`、行高 `1.7`,答案 `26px` 且配色强调。
* 每个小标题(`接続` / `意味` / `用例`)带左色条 + 文字,不靠颜色单独传达含义。
* 挖空的 `（　　）` 用 `span.blank` 强调色;例句用 `.ex` 面板色块区分。
* 字段内容一律 HTML 转义,避免例句里的 `<` `&` 破坏卡片。
* 字体链含 `Noto Sans JP` / `Hiragino Kaku Gothic ProN` / `Yu Gothic` / `Meiryo` 回退。

验证方式:把三个模板渲染成 HTML,用 headless Chrome 分别在
`.card.nightMode` 和 `--force-dark-mode`(即 `prefers-color-scheme: dark`)下截图对比,
两条路径都正常。

## 常驻优化循环(loop)

免费额度不用白不用 —— `src/loop.py` 是一个**常驻循环**,把额度一直吃满,慢慢把质量磨上去。

```bash
python3 src/loop.py                  # 一直跑(推荐 setsid nohup 挂后台)
python3 src/loop.py --once           # 只跑一轮,先看看它要干什么
python3 src/loop.py --rounds 5       # 最多 5 轮
python3 src/loop.py --sleep 300      # 没活可干时休眠 5 分钟再查
```

一轮做什么:

```
expand   → 把 status=new 的补上(扩写)
verify   → 把 status=expanded 的自检
qc --dry → 程序化质检(不花额度,先看命中多少)
kana     → 专门修假名注音(双模型投票,只采纳一致的)
qc       → 修完再查一遍,剩下的写成 review 喂给 repair
repair   → 只修被标记的行
收敛判断 → 同一文法点连续 3 轮没修好 → 自动升级到强模型;再不行标 stuck 交人工
```

### 可中断 / 可复联

| 想要 | 怎么做 |
|---|---|
| 跑完当前阶段后优雅退出 | `touch data/out/loop.stop` 或按 Ctrl-C |
| 随时看进度 | `cat data/out/loop_status.json`(轮数、状态分布、今日用量) |
| 看完整日志 | `tail -f data/out/logs/loop.log` |
| 断了接着跑 | 直接再执行 `python3 src/loop.py`,状态全在 SQLite 里,不会重头来 |
| 额度用满 | 循环自动 sleep 再查,跨过分钟/跨过天会自动接上 |
| 卡住的条目 | `loop_state.stuck=1` + `status=needs_review`,留给人工队列 |
| 防止重复启动 | 启动时写 `data/out/loop.lock`(存 PID);已有活着的 loop 就拒绝启动 |

> **只能跑一个 loop。** 两个实例会重复消耗配额并并发写 SQLite。锁文件里的 PID 已死时
> 会视为陈旧锁自动接管,所以进程被 `kill -9` 也能重跑。

**所有进度都在数据库里**(`grammar_point.status`、`review`、`loop_state`),
进程可以被随便杀掉,不会丢工作、也不会重复劳动。

### 状态机

```
new ──expand──> expanded ──verify──> verified ──build_cards──> done
                    ↑                    │
                    └──── repair ────────┘   (needs_review / qc:rule / kana:vote)
```

## 原则

1. SQLite 是唯一真相来源;Anki 随时可重建。
2. 每条 AI 内容带 `confidence` 和 `prompt_version`;`uncertain` 不进卡片。
3. 全流程幂等、可断点续跑:只处理状态为待处理的行,失败的行可单独重跑。
4. 书里的信息优先于 AI;冲突时以书为准并记录。
5. 人工只审被标记的部分。
6. 只存文法点名称、页码索引和 AI 用自己的话写的扩写,不抄录书籍原文。

## confidence 分级

| 值 | 含义 | 是否进卡 |
|---|---|---|
| `established` | 辞典/语法书普遍认可 | ✅ |
| `common_claim` | 常见说法,来源不一或有争议 | ✅(语源类标注「通説」) |
| `mnemonic_only` | 只是帮助记忆的联想 | ⚠ 只作「联想记忆」字段 |
| `uncertain` | 没把握 | ❌ |

## 状态机

```
grammar_point.status
  new ──expand──> expanded ──verify──> verified ──build_cards──> done
                     │                    │
                     │                    └──> needs_review ──repair──> expanded(重审)
                     └── review_batch(外部模型) ─────────────────┘
                                                          └──人工审核──> verified
```

review 表里的 `verdict` 分 `ok / doubtful / wrong`;`expansion` 和 `example` 分开记,
`review_batch.py stats` 把多审查者意见不一致的条目自动置为 `needs_review`。

## 持续优化

* `eval/golden.json` 里放 10 个不同难度的标准答案(现已有 3 条种子,`confirmed_by` 待人工确认),
  改提示词后先跑它:`python3 src/eval_golden.py`。
* 换提示词:`prompts/expand_v2.md` → `python3 src/expand.py --status expanded --new-version`;
  旧内容保留(按 `prompt_version` 区分,旧行追加 `:superseded`)。
* 修复循环:被标 `doubtful/wrong` 的行 → `src/repair.py` 定向修复 → 再 `src/verify.py` 重审。
  实测 28 条 → 一轮后剩 7 条 → 二轮后剩 6 条 → 换成更强模型(gemini-pro)后归零。
* 在 Anki 里给有问题的卡打 `check` 标签,定期回流把对应文法点重置为 `needs_review`。

## 卡片类型

学习方向是 **日语 → 中文意思**(読解导向),不做「中文 → 日语」的产出卡。
原因:N1 文型主要出现在书面语/文章中,日常生活中基本不用,
所以需要练的是「读到能认出、能懂」,而不是「想得到、能写出来」。

| 卡型 | 正面(日语) | 背面 |
|---|---|---|
| **読解卡** `reading` | 例句(文型用蓝色高亮标出)+「文の意味をつかむ」 | 中文意思 → 訳(例句+假名+中文) → 文型 → 接続 → 用法 → 使い分け |
| **文型卡** `point` | 「この文型の意味は？」+ 文型 | 中文意思 → 読み → 接続 → 例文 → 用法 → 使い分け |
| **联想卡** `etymology_hook` | 「漢字で書くと？ 由来は？」+ 文型 | 漢字表記 / 由来(标定説・通説・联想记忆)/ 意味 |

联想卡只在有 `kanji_writing` 或 `etymology` 且非 `uncertain` 时生成。

### 对应的 Anki 笔记类型

| card_type | Anki 笔记类型名 | model id | 字段数 |
|---|---|---|---|
| `reading` | `文法-読解` | 660591022 | 10 |
| `point` | `文法-文型` | 2412699645 | 7 |
| `etymology_hook` | `文法-联想` | 45774564 | 7 |

model id 在 `export_anki.MODEL_IDS` 里**写死**。Anki 认的是 id 不是名字,所以改名字
不会新建笔记类型。早期这 3 个叫 `N1文法-読解` 等(N1 前缀),收录 N2 后改回通用名。

### 牌组与标签

| 来源 | 牌组(deck id 固定) | note 标签 |
|---|---|---|
| 日本語総まとめ N1(第1〜4週) | `日本語文法::N1` (2059400111) | `文法`·`文法::N1`·`文法::N1::第1週1日目` |
| JLPT N2 清单 | `日本語文法::N2` (2059400112) | `文法`·`文法::N2` |

牌组用 `::` 嵌套(Anki 侧边栏里归到一个「日本語文法」下面);标签树同理,
方便按天/按级别过滤。名字定义在 `export_anki.DECK_MAP` / `tags_for()`,
**改名字不影响更新**(Anki 认 deck id 不认名字)。

### 升级 / 重导入(Anki 的实际规则)

官方文档:[Packaged Decks](https://docs.ankiweb.net/importing/packaged-decks.html#updating)、
[FAQ: note type has changed](https://faqs.ankiweb.net/some-updates-were-ignored-because-the-note-type-has-changed.html)。

1. 匹配靠 **note guid**(`anki_guid` 由 `grammar_id + card_type` 稳定生成)→ 同名即同一张,不重复。
2. 默认**只在新包的 note 更新(mtime 更大)时才更新**已有 note。
3. **note type 变了就更新不了**:要求「字段名/数量/顺序不变」+「模板名/数量/顺序不变」。
   只改模板里的正反面 HTML **不会**阻止更新。
4. 导入**永远不会删**你库里的 note —— 新包里不再存在的卡只能在 Anki 里手动删。

所以:

* **只改内容**(字段和模板都没动)→ 直接导入新 `.apkg`,原地更新,学习进度保留。
* **改了字段或模板**(加字段、改模板名)→ 默认导入不会更新旧笔记,还会留下带后缀的
  笔记类型(如 `文法-読解-a4f68`)。干净做法:
  1. 删掉旧牌组(早期叫 `日本語総まとめ N1 文法`,现在叫 `日本語文法::N1`),重启 Anki;
  2. `工具 → 管理笔记类型` 删掉不再用的 `N1文法-*`;
  3. 再导入新 `.apkg`。
* **想让合并靠 id 而不是名字**:genanki 目前不写 field/template id,Anki 只能按名字比对。

> 规矩:**要能原地更新就得冻结 schema** —— 字段名/数量/顺序、模板名/数量/顺序都别再动,
> 只改字段内容或模板里的 HTML。

## 例句朗读音频(VOICEVOX)

给每个文法点的**前 2 条例句**加朗读音。

```bash
python3 src/tts.py --workers 4     # 全量 547 条,约 1 分钟
python3 src/tts.py --limit 5 --dry  # 只看会做什么,不合成
python3 src/tts.py --gc            # 顺手删掉不再被引用的残留 mp3
```

- 音色在 **四国めたん(2)/ 波音リツ(9)/ ずんだもん(3)** 里按例句 id **稳定随机**
  —— 同一句永远同一个音色,重跑不变。`--reshuffle` 重洗,`--speaker N` 固定。
- ffmpeg 从 **stdin 管道**读 wav 直接转 mp3, **wav 全程不落盘**;24kHz 单声道 VBR q5,
  547 条约 13MB。
- 文件名 = `sha1(文本+音色+参数)[:16].mp3` → 内容寻址,幂等;`example.audio` 落了库,
  重跑直接跳过(实测重跑 0.3s)。
- 需要 VOICEVOX engine 在 `127.0.0.1:50021`(可用 `VOICEVOX_URL` 覆盖)+ 系统里有 ffmpeg。

**卡片上怎么播**:答案面里第 1 句用 Anki 原生的 `[sound:]` → **自动播放**;
第 2 句用 `<audio>` + 「▶ 再听」按钮 → **点了才播**。之所以不用两个 `[sound:]`,
是因为 Anki 会把一张卡上的多个 sound **依次连播**,做不到「只自动播第一句」。

音频由 `export_anki.py` 收集(`genanki.Package(media_files=...)`)打进 `.apkg`。
`pipeline.sh` 里已加这一步,VOICEVOX 没起就自动跳过。

## 术语线:特定技能教材 → 单词牌组

同一套「SQLite 为真相源」做法,吃两本教材 PDF(`pic-sourse/` 下的学習用テキスト + サンプル問題):

```
src/pdf2md.py --ruby     # PDF → md(振り仮名解析,页码注释)
src/pdf_vocab.py ingest  # 挖词入 term 表(读音/类别/★难度/出现次数/页码)
src/term_note_fill.py    # LLM 填 zh + note(term_note_v1;uncertain 不出卡)
src/term_tts.py          # VOICEVOX 逐词朗读(内容寻址,重跑跳过)
src/term_triage.py       # 噪声清理白名单 + 重要度打分/分档
src/term_cards.py        # 出 data/out/特定技能用語.apkg
```

- **重要度打分**:`hits`(教材出现次数)/ ★难度 / 片假名外来语 / 常用词表 / 领域加权
  → 写进 `term.priority`,按名次切档 `term.tier`:1=必須(top 300)/ 2=重要(600)/ 3=補完。
  纯程序化可重跑;分档常数在 `term_triage.py` 顶部。
- **改名不丢进度**:改词形/读音时把旧 `term:reading` 写进 `term.guid_key`,
  GUID 保持不变 → Anki 重导入是**原地更新**。
- **剔除词只发一次**:`status='skip'` 的噪声词带 `用語::削除候補` 标签随下一次包发一遍
  (`skip_sent=1`),在 Anki 里搜该标签一键删;以后不再发。
- **Anki 重导入实测行为(26.9)**:字段/标签原地更新、学习进度保留,
  **但不会把已有卡片迁到新牌组** —— 换牌组布局后需要在浏览器里
  按标签搜索 + 「カードを変更/Change Deck」来迁移。
- 交付与导入步骤见 `data/out/特定技能用語_导入指引.md`。

## 当前结果

数据库是唯一真相来源,状态会随 loop 变化。**随时用下面命令看当前快照**,
不要以本文里的数字为准:

```bash
sqlite3 db/grammar.sqlite 'SELECT status, COUNT(*) FROM grammar_point GROUP BY status;'
python3 src/review_queue.py            # 待人工审核队列
python3 src/loop.py --once             # 或跑一轮会打印状态分布
```

历史背景:

* N1 部分收录 **第1週〜第4週共 74 个文法点**(第5週・第6週目录照片尚未提供),来自
  `pic-sourse/IMG20260929*.jpg` 的 OCR 结果。
* N2 部分由 `src/fetch_n2.py` 抓取,共 200 个文法点,`book_order` 从 1000 起。
* N1 那批曾全量跑到 `verified = 74`、导出 220 张卡(読解 74 / 文型 74 / 联想 72)。
* 产物:`data/out/日本語文法.apkg` —— 两个牌组:`日本語文法::N1`
  (deck_id=2059400111)/ `日本語文法::N2`(deck_id=2059400112),共 **806 张**
  (読解 274 / 文型 274 / 联想 258)+ 547 个例句朗读 mp3。
  牌组 id 固定不变,只是改了显示名;两边都能靠 id 对上是同一个牌组。
* 旧的 `data/out/N1文法.apkg`(旧 schema)已归档到 `data/out/archive/`,**别再导入**。
* 幂等性已验:`build_cards.py` 重跑后 `card.version` 从 1 → 2 而不是新增行;
  `anki_guid` 稳定,再导入 Anki 是更新而不是重复。
  > 注:加入 N2 后,`build_cards` 全量模式只保留仍处于 `verified`/`done` 的卡,
  > 退出 `verified` 的文法点旧卡会从 `card` 表清掉(Anki 里的旧笔记不受影响)。

### 本次跑出来的问题(已修)

| 问题 | 发现方式 | 处理 |
|---|---|---|
| TOC 把「〜ずくめ」读成了「〜すくめ」(漏浊音) | verify 挑错 | 修 `toc_ocr.json`;`ingest` 改用 `(chapter, page_ref)` 做幂等键,标题改了也 UPDATE 而非插重复行 |
| 例句出现乱码字(`つéたまま`、`嫌가っている`) | verify 挑错 | repair 定向重写 |
| 假名注音错(`こたつわる`→`よこたわる`、`こんげ`→`こんげつ`) | verify 挑错 | repair 定向重写 |
| 接续/助词错(`お礼がかたがた`、`知るだになかった`) | verify 挑错 | repair 定向重写 |
| 免费模型 429 限流 | 日志里成片 ERR | 加模型容灾链(见上) |

## 数据来源与版权

- **文法点清单**:N1 来自《日本語総まとめ N1 文法》的目录页(只取文法点名称 / 读音 /
  页码索引);N2 来自 [japanesetest4you.com 的 JLPT N2 list](https://japanesetest4you.com/jlpt-n2-grammar-list/)
  (`input/toc/n2_list.json` 里保留了原链接)。
- **例句与讲解全部是 AI 生成的原创内容** —— 数据库里 `example.origin` 全是 `ai`,
  没有抄书的正文或书上的例句。
- **不包含书的照片**:`pic-sourse/` 已进 `.gitignore`,不会上传(书的照片有版权)。
- 本项目只是“把书的结构当索引 + 用 AI 自己写学习材料”的工具与成品,
  请配合原书使用;若权利方有异议,删掉相关内容即可。
- 代码 [MIT](LICENSE);生成的语法数据供个人学习使用。
