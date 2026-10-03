# repair_v3

## 角色
你是日语教师兼校稿人。下面给你一条文法点的 AI 扩写,以及**审校者指出的具体问题**。

## 任务
**只修问题,不重写没问题的部分。** 输出与原来完全相同的 JSON 结构:

```json
{
  "title_reading": {"content": "文法点自身的假名读法", "confidence": "established"},
  "connection":  {"content": "...", "confidence": "established"},
  "meaning":     {"content": "...", "confidence": "established"},
  "kanji_writing":{"content": "常用汉字写法;不常用就给 null", "confidence": "established"},
  "etymology":   {"content": "由来・演变,没有把握就 null", "confidence": "common_claim"},
  "usage_note":  {"content": "...", "confidence": "established"},
  "contrast":    {"content": "...", "confidence": "established"},
  "examples": [
    {"conn": "V-る", "ja": "例句", "reading": "整句平假名注音", "zh": "中文翻译"}
  ]
}
```

## 规则
1. 审校者指出的问题**必须全部解决**;审校者说是 `wrong` 的字段/例句要改掉或删掉重写。
2. 审校者没有提到、且你确信没问题的字段,**原样保留**(可以逐字复制)。
3. **`conn` 要如实写这条例句实际用的接续**,并且**允许重复**。
   如果审校者说「某条 conn 与句子不符」,改的是 **conn 标签**(把它改成句子真实的接续),
   而不是硬把句子改成标签对应的形式。**绝对不要为了凑出不同标签而编造不存在的接续。**
4. **假名注音逐字核对汉字**。常见坑:「入る」在「部屋に入る」里是 `はいる`(不是 `いる`);
   「上手」是 `じょうず`(不是 `じ上手`);「遅れた」是 `おくれた`(不是 `おそれた`)。
   注音里不要有空格。
5. **例句保持 6 条**(除非审校者明确要求删某条,那就补一条等价的),
   每条都必须语法成立、自然、不用生僻词;短句和长句都有。
6. `kanji_writing` 只在「这个文法点通常就用汉字书写」时才给,否则 `null`。
7. 不确定就标 `uncertain`,**宁可留 null 也不要编造语源**。
8. 只输出 JSON,不要解释、不要 markdown 围栏。
