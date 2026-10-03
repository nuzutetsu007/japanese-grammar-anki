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
