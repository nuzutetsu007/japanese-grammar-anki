# PLAN

> 状态基线(2026-09-29):
> 收录 第1〜4週 / 74 文型,`verified 74 / needs_review 0`。
> 卡片 220 张 = 読解 74 + 文型 74 + 联想 72(方向:**日语 → 中文意思**)。
> 产物 `data/out/N1文法.apkg`(已重新导出,用户已删旧牌组,待导入)。

## 已定决策

| 决策 | 结论 | 日期 |
|---|---|---|
| 学习方向 | **日语 → 中文意思**;删掉「中文 → 日语」产出卡(N1 文型是读解用,生活中不用) | 09-29 |
| 読解卡信息量 | **不瘦身**,保留 意思+訳+文型+接続+用法+使い分け | 09-29 |
| 第5週・第6週照片 | **四周后(学完前四周)再补** | 09-29 |
| 旧 apkg | 用户已手动删除旧牌组,直接导入新包 | 09-29 |

---

## P0 — 收尾当前方向修正 ✅ 已完成

- [x] `README.md` 新增「卡片类型」章节:reading / point / etymology_hook 三类,
      写清为什么不做产出卡;附 card_type ↔ Anki 笔记类型名对照。
- [x] `README.md` 新增「升级 / 重导入」小节(旧 note type 会残留,先删牌组再导入)。
- [x] `src/sample_check.py` 写好并跑通:随机抽 N 条 → `data/out/sample_check.md`
      (人看)+ `sample_check.json`(带空 verdict,方便标记回流)。
      已加入 README 快速开始与 src 清单。
- [x] `data/out/N1文法.apkg` 重新导出并解包校验(220 notes / guid 全唯一 / 3 个 note type)。

## P1 — 补数据(等你)

- [ ] **补拍 第5週・第6週 目录照片**(约四周后)→ `pic-sourse/`
      → 追加进 `input/toc/toc_ocr.json` → `python3 src/ingest.py`
      (幂等,不动已有 74 条)→ expand → verify → repair → build → export。
      全书预计 110 文型左右。
- [ ] **人眼过一遍抽检清单**:`data/out/sample_check.md`(seed=0 的 10 条),
      把问题写在文件里 → 我按类型改提示词。
- [ ] **黄金样本扩到 10 条**:`eval/golden.json` 现在 3 条且 `confirmed_by: null`,
      确认后填上(改提示词前回归的前提)。

## P2 — 质量与体验

- [ ] **近义文型互链**:`〜すら/〜ですら`↔`〜だに`、`〜が早いか`↔`〜や否や` 等,
      卡背自动加「関連: #123 〜だに」跳转。
- [ ] **外部模型校准**:`src/review_batch.py export` 已产出 `review_batch.json`,
      还没给第二个模型审。跑一轮 → `import` → `stats`,分歧项进人工队列。
      (可复用 `pi` CLI,换一个模型当第二审。)

## P3 — 长期循环

- [ ] **Anki 反馈回流**:`src/feedback.py`,读 Anki 导出笔记里的 `check` 标签,
      把对应文法点重置为 `needs_review`。
- [ ] **TTS 音频**(可选):読解卡例句加发音,media + `[sound:xxx.mp3]`。
- [ ] **`make check`**:编译所有 src + `eval_golden.py` + apkg 完整性校验
      (note 数 / guid 唯一 / 三种 note type 齐)。

---

## 建议执行顺序

```
P0 ✅ → 你过一遍 sample_check.md → 四周后补拍 → ingest/expand/verify/build/export → P2 → P3
```
