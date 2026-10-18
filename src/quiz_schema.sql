-- 特定技能(電気電子機器組立て)练习题库 —— 临时线,考完就删。
-- 与 db/grammar.sqlite 无关,这不是长期真相源:结构要改就直接改这里 + 重建库。
--
-- 用法(.venv/bin/python):
--   src/quiz_gen.py init                     # 建库(幂等)
--   src/quiz_official.py --source both       # 官方素材解析 → JSONL
--   src/quiz_gen.py import data/out/quiz_*.jsonl
--   src/quiz_gen.py chunks                   # 教材正文切块(出题原料)
--   src/quiz_gen.py gen --kind ox --workers 4
--   src/quiz_web.py                          # 浏览器刷题 http://127.0.0.1:8765

PRAGMA journal_mode=WAL;

-- 出题原料:教材正文切块(只存解析结果,可随时重切)
CREATE TABLE IF NOT EXISTS source_chunk (
  id         INTEGER PRIMARY KEY,
  chapter    TEXT NOT NULL,              -- 章名,如「安全衛生」
  ordinal    INTEGER NOT NULL DEFAULT 0, -- 章内顺序
  page_from  INTEGER NOT NULL DEFAULT 0, -- 教材页码(闭区间)
  page_to    INTEGER NOT NULL DEFAULT 0,
  text       TEXT NOT NULL,
  n_chars    INTEGER NOT NULL DEFAULT 0,
  hash       TEXT NOT NULL UNIQUE,       -- sha1(chapter+text) → 重切幂等
  created_at TEXT
);

-- 题库:学员看到的题目(官方素材 + AI 生成)
-- kind    ox=学科真偽法(A=正しい/B=間違い) choice=実技4択(A-D)
-- origin  official=2025年度サンプル問題 textbook=教材練習問題 ai=模型生成
-- 可用口径(网页端统一):confidence<>'uncertain' 且 review_verdict<>'wrong' 且 has_figure=0
CREATE TABLE IF NOT EXISTS question (
  id             INTEGER PRIMARY KEY,
  kind           TEXT NOT NULL,
  stem           TEXT NOT NULL,
  choices        TEXT NOT NULL DEFAULT '[]',  -- JSON 数组;ox 为 []
  answer         TEXT NOT NULL,
  explanation_zh TEXT,                        -- 中文解释(explain 后填)
  explanation_ja TEXT,                        -- 教材日语原解说(仅 textbook 来源)
  chapter        TEXT NOT NULL DEFAULT '',
  difficulty     INTEGER NOT NULL DEFAULT 0,  -- 1-5;官方素材为 0
  pages          TEXT NOT NULL DEFAULT '',
  origin         TEXT NOT NULL DEFAULT 'ai',
  ref            TEXT NOT NULL DEFAULT '',    -- 「2025年度サンプル 問題12」「練習問題 2-1 ①」
  has_figure     INTEGER NOT NULL DEFAULT 0,  -- 依赖图/照片 → 网页默认不出
  confidence     TEXT,
  prompt_version TEXT,
  model          TEXT,
  explain_model  TEXT, explain_version TEXT,
  chunk_id       INTEGER REFERENCES source_chunk(id),
  dedup_key      TEXT NOT NULL UNIQUE,        -- sha1(kind+题干去空白) → 导入去重
  review_verdict TEXT,                        -- ok/doubtful/wrong(第二个模型审)
  review_model   TEXT, review_version TEXT, review_note TEXT,
  created_at     TEXT, updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_question_chapter ON question(chapter, kind);
CREATE INDEX IF NOT EXISTS idx_question_usable  ON question(has_figure, confidence, review_verdict);

-- 答题记录(错题本/统计都从这里算)
CREATE TABLE IF NOT EXISTS attempt (
  id          INTEGER PRIMARY KEY,
  question_id INTEGER NOT NULL REFERENCES question(id),
  session_id  INTEGER,
  chosen      TEXT NOT NULL,
  correct     INTEGER NOT NULL,   -- 0/1
  elapsed_ms  INTEGER,
  ts          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_attempt_q ON attempt(question_id, ts);
CREATE INDEX IF NOT EXISTS idx_attempt_s ON attempt(session_id);

-- 模拟考/练习场次
CREATE TABLE IF NOT EXISTS session (
  id          INTEGER PRIMARY KEY,
  mode        TEXT NOT NULL,        -- exam / practice / wrong
  n           INTEGER NOT NULL DEFAULT 0,
  n_correct   INTEGER NOT NULL DEFAULT 0,
  score       REAL,
  passed      INTEGER,
  detail      TEXT,                 -- JSON:学科/実技拆分等
  started_at  TEXT, finished_at TEXT
);

-- 运行记录(照项目老规矩)
CREATE TABLE IF NOT EXISTS run_log (
  id             INTEGER PRIMARY KEY,
  stage          TEXT NOT NULL,
  prompt_version TEXT,
  started_at     TEXT, finished_at TEXT,
  rows_ok        INTEGER DEFAULT 0,
  rows_failed    INTEGER DEFAULT 0,
  note           TEXT
);
