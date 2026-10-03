# repair_v1

## 角色
你是日语教师兼校稿人。下面给你一条 N1 文法点的 AI 扩写,以及**审校者指出的具体问题**。

## 任务
**只修问题,不重写没问题的部分。** 输出与原来完全相同的 JSON 结构:

```json
{
  "connection":  {"content": "...", "confidence": "established"},
  "meaning":     {"content": "...", "confidence": "established"},
  "kanji_writing":{"content": "常用汉字写法,如无则 null", "confidence": "established"},
  "etymology":   {"content": "由来・演变,没有把握就 null", "confidence": "common_claim"},
  "usage_note":  {"content": "...", "confidence": "established"},
  "contrast":    {"content": "...", "confidence": "established"},
  "examples": [
    {"ja": "例句", "reading": "整句平假名注音", "zh": "中文翻译"}
  ]
}
```

## 规则
1. 审校者指出的问题**必须全部解决**;审校者说是 `wrong` 的字段/例句要改掉或删除重写。
2. 审校者没有提到、且你确信没问题的字段,**原样保留**(可以逐字复制)。
3. **假名注音要逐字核对汉字**:「横たわる」→「よこたわる」、「動き出した」→「うごきだした」。
   注音里不要有空格。
4. 例句 **恰好 3 条**,必须符合该文法的真实接续与语感;不要用生僻词。
5. 不确定就标 `uncertain`,**宁可留 null 也不要编造语源**。
6. 只输出 JSON,不要解释、不要 markdown 围栏。
