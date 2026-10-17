-- 文法点(目录里读出来的)
CREATE TABLE IF NOT EXISTS grammar_point (
  id INTEGER PRIMARY KEY,
  book_order INTEGER,            -- 书中顺序
  chapter TEXT,
  title_ja TEXT NOT NULL,        -- 如「〜ので」
  reading TEXT,                  -- 假名读音(AI 生成,或目录里直接读到的)
  reading_romaji TEXT,           -- 来源页给的罗马字读音(如 aruiwa),仅作参考
  page_ref TEXT,                 -- 页码或来源 URL,方便回原处查
  status TEXT DEFAULT 'new'      -- new / expanded / verified / needs_review / done
);

-- 来自书本/来源页的事实(可选)
CREATE TABLE IF NOT EXISTS book_fact (
  id INTEGER PRIMARY KEY,
  grammar_id INTEGER REFERENCES grammar_point(id),
  connection TEXT,               -- 接续
  meaning_zh TEXT,               -- 意思(用自己的话概括,不抄原文)
  source_gloss TEXT,             -- 来源页给的英文释义(如 "or, possibly"),仅作参考
  source_image TEXT              -- 照片文件名 / URL
);

-- AI 扩写的内容,按字段拆开存
CREATE TABLE IF NOT EXISTS expansion (
  id INTEGER PRIMARY KEY,
  grammar_id INTEGER REFERENCES grammar_point(id),
  field TEXT NOT NULL,           -- connection / meaning / kanji_writing / etymology / usage_note / contrast
  content TEXT NOT NULL,
  confidence TEXT NOT NULL,      -- established / common_claim / mnemonic_only / uncertain
  model TEXT,
  prompt_version TEXT,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- 例句
CREATE TABLE IF NOT EXISTS example (
  id INTEGER PRIMARY KEY,
  grammar_id INTEGER REFERENCES grammar_point(id),
  ja TEXT NOT NULL,
  reading TEXT,
  zh TEXT,
  conn TEXT,                     -- 这条例句展示的是哪种接续(如 V-た / N＋の)
  origin TEXT DEFAULT 'ai',      -- ai / book
  verified INTEGER DEFAULT 0,    -- 0=未核对 1=已人工/外部核对
  model TEXT,
  prompt_version TEXT,
  audio TEXT,                    -- 朗读 mp3 文件名(在 data/out/media/ 里),空的=没生成
  audio_speaker INTEGER          -- 生成时用的 VOICEVOX speaker id(稳定随机的结果)
);

-- 审核记录(自检、其他模型、人工都记在这里)
CREATE TABLE IF NOT EXISTS review (
  id INTEGER PRIMARY KEY,
  target_table TEXT,             -- expansion / example
  target_id INTEGER,
  reviewer TEXT,                 -- self / model:<name> / human
  verdict TEXT,                  -- ok / doubtful / wrong
  comment TEXT,
  prompt_version TEXT,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- 最终卡片
CREATE TABLE IF NOT EXISTS card (
  id INTEGER PRIMARY KEY,
  grammar_id INTEGER REFERENCES grammar_point(id),
  card_type TEXT,                -- recognition / production / etymology_hook
  front TEXT, back TEXT,
  anki_guid TEXT UNIQUE,         -- 稳定ID,重导入时更新而不是重复
  version INTEGER DEFAULT 1,
  status TEXT DEFAULT 'draft'    -- draft / approved / exported
);

-- 运行日志,方便断点续跑和排查
CREATE TABLE IF NOT EXISTS run_log (
  id INTEGER PRIMARY KEY,
  stage TEXT, prompt_version TEXT, started_at TEXT, finished_at TEXT,
  rows_ok INTEGER, rows_failed INTEGER, note TEXT
);

CREATE INDEX IF NOT EXISTS idx_expansion_grammar ON expansion(grammar_id);
CREATE INDEX IF NOT EXISTS idx_example_grammar ON example(grammar_id);
CREATE INDEX IF NOT EXISTS idx_review_target ON review(target_table, target_id);
CREATE INDEX IF NOT EXISTS idx_card_grammar ON card(grammar_id);

-- 常驻优化循环的状态(可中断/可复联:一切以这张表为准)
CREATE TABLE IF NOT EXISTS loop_state (
  grammar_id INTEGER PRIMARY KEY REFERENCES grammar_point(id),
  tries      INTEGER DEFAULT 0,   -- 被循环处理过几轮
  stuck      INTEGER DEFAULT 0,   -- 1=连续多轮没修好,交给强模型/人工
  last_action TEXT,
  last_verdict TEXT,
  updated_at TEXT
);

-- ============================================================================
-- 术语表(特定技能 製造分野 教材 PDF 的词汇;pic-sourse/ 里那两份)
-- 只存 词形/读音/页码/我们自己定的分类与难度 —— 不存书里的任何句子。
-- 难度/分类是程序化结果,可重跑覆盖;zh(中文)/note(说明)是后填的 AI 内容,
-- 重跑不覆盖(靠 note_version 区分版本)。md 报告只是这张表的导出物。
-- status: new(待挑) → keep(要做卡) / skip(不要) → carded(已出卡)
-- ============================================================================
CREATE TABLE IF NOT EXISTS term (
  id          INTEGER PRIMARY KEY,
  term        TEXT NOT NULL,
  reading     TEXT NOT NULL DEFAULT '',      -- 读音(来自书里的振り仮名);片假名词留空
  kind        TEXT NOT NULL DEFAULT 'kanji', -- kanji / kata
  category    TEXT NOT NULL DEFAULT 'その他',
  difficulty  INTEGER NOT NULL DEFAULT 0,    -- 1..5
  zipf_min    REAL,                          -- 难度依据:词内最罕用成分的日语 Zipf
  zipf_whole  REAL,
  in_wordlist INTEGER NOT NULL DEFAULT 0,
  hits        INTEGER NOT NULL DEFAULT 0,    -- 在教材里出现次数
  pages       TEXT NOT NULL DEFAULT '',      -- 出现页(逗号分隔)
  source      TEXT NOT NULL DEFAULT '',      -- 来源 PDF 文件名
  status      TEXT NOT NULL DEFAULT 'new',
  zh          TEXT,                          -- 中文(后填)
  note        TEXT,                          -- 一句话说明(后填)
  confidence  TEXT,                          -- 说明的置信度
  note_model  TEXT, note_version TEXT,       -- AI 内容的模型 / 提示词版本
  audio        TEXT,                          -- 朗读 mp3 文件名(内容寻址,放 data/out/media)
  audio_speaker INTEGER,                      -- 用的哪个音色(重跑不变)
  extract_version TEXT,                      -- 抽取器版本(重跑换版本号)
  priority    REAL,                          -- 重要度打分(程序化,term_triage.py 可重跑覆盖)
  tier        TEXT,                          -- '1'=必須 / '2'=重要 / '3'=補完(按 priority 名次切档)
  guid_key    TEXT,                          -- 首发 GUID 用的 "term:reading";改名/改读音后仍用它算 GUID
  skip_sent   INTEGER NOT NULL DEFAULT 0,    -- 被剔除(skip)的词是否已随包发过一次「削除候補」
  created_at  TEXT, updated_at  TEXT,
  UNIQUE(term, reading, source)
);
CREATE INDEX IF NOT EXISTS idx_term_cat    ON term(category, difficulty);
CREATE INDEX IF NOT EXISTS idx_term_status ON term(status);
