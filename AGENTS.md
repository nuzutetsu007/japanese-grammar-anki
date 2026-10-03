# 日语文法书 → SQLite → Anki(给 agent 的工作说明)

从目录照片/文法清单出发,建一个可迭代的文法数据库,用 AI 扩写,最后导出 Anki。
**SQLite 是唯一真相来源,Anki 只是导出物,随时可重建。**

## 铁律(违反 = 数据不可信)

1. 每条 AI 内容都带 `confidence` + `prompt_version`;重跑用新版本号,**绝不覆盖旧行**。
2. `uncertain` 不进卡片;`mnemonic_only` 只能当「联想记忆」,不能当正式释义/接续。
3. 全流程幂等、可断点续跑:只处理待处理状态的行;进度全在库里,进程可随便杀。
4. 书里的信息优先于 AI;冲突时以书为准并记录。
5. 人工只审**被标记的**部分,不审全部。
6. 不抄书原文:只存文法点名称、页码索引、以及 AI 用自己的话写的扩写。
7. `loop.py` 同一时刻只能跑一个实例(有 PID 锁);并发写库会互相破坏。

## 目录与真相位置

```
input/toc/        目录 OCR 结果 / N2 清单(照片在 pic-sourse/)
db/grammar.sqlite 唯一真相来源
src/schema.sql    表结构真相 —— 改结构改这里,并在 common._MIGRATIONS 补迁移
prompts/*_vN.md   提示词按版本管理,改提示词 = 新建一个版本文件
src/common.py     DB / run_log / 模型调用(HTTP,配额调度 + 容灾)
src/*.py          各阶段脚本;产物与日志在 data/out/
eval/golden.json  改提示词前的标准答案样本
```

## 数据模型要点(细节看 `src/schema.sql`)

- `grammar_point.status`: `new → expanded → verified → done`;出问题回 `needs_review`。
- `expansion` 按字段拆开存、带 confidence;`example` 带假名 / 中文 / 接续标签。
- `review` 记所有审查(自检 / 外部模型 / 人工 / 程序化质检),靠 `verdict` 汇总。
- `card.anki_guid` 由 `grammar_id + card_type` 稳定生成 → 重导入是**更新**不是新增。
- `loop_state` 是常驻循环的断点(`tries` / `stuck`)。

## 流水线(一步一件事,失败可单跑)

`ingest → expand → verify → qc/kana → repair → review_queue → build_cards → export_anki`

- 「下一步做什么」永远以**库里的状态**为准,不要靠内存或日志推断。
- 每阶段结束往 `run_log` 写一条。
- 关键阶段失败要中止/重试,不要静默继续(见 `pipeline.sh` 的 `run_or_die`)。
- **增量别重写已验证内容**:`pipeline.sh` 默认只扩 `new`;要全量重跑旧内容得显式
  `REEXPAND=1`,否则会白白作废好数据。

## 模型调用启发式

- 不 shell `pi`,直接 HTTP(`common.chat`,OpenAI 兼容端点);key 放 `.env`(权限 600)。
- **按输出大小分派**:大输出(扩写)给 TPM 大的 provider,小输出(verify)给带 OTPM 的。
- 声明值超过 OTPM 的请求直接跳过,别发出去反复重试。
- 遇到 429/5xx:按建议时间冷却该模型、换下一个,不要原地死等。
- 例句注音最容易崩 → 拆成单独任务、双模型投票,只采纳一致的(见 `kana.py`)。

## 提示词启发式(不要笼统说「更准确」)

- 角色要具体:日语教师 / 挑剔的母语审校者 / 只做注音的工具。
- 只输出 JSON,字段和 confidence 一起给;拿不准就 `uncertain`,**宁可留 null 也不要编语源**。
- 允许 `conn` 重复,不要为了凑标签编造不存在的接续。
- 按**错误类型**改提示词(接续错 / 例句不自然 / 语源编造 / 注音错),一次改一类。
- 改完先在 `eval/golden.json` 上跑,过了再全量重跑。

## 常驻循环的启发式

- 吃满免费额度,但可随时 Ctrl-C 或 `touch data/out/loop.stop`,状态全在库。
- 每轮:expand 缺的 → verify 待审的 → qc/kana 修硬伤 → repair 被标记的。
- 同一文法点连续几轮修不好 → 升级强模型;再不行标 `stuck` 交人工,别无限震荡。
- 判断「有没有进展」只看库里的状态,不要相信内存计数。

## 起步

1. `python3 src/init_db.py`
2. `python3 src/ingest.py --list` → 人工确认一次清单(数量 / 漏页 / OCR 错字)
3. `python3 src/expand.py --limit 3` + `verify.py`,看格式和质量
4. 确认后:`python3 src/loop.py`(后台常驻,单实例)或 `bash src/pipeline.sh`(增量)
