# 材料、题目与来源

## 来源等级

`official`：已核验的考试机构公告或大纲，保存 URL、访问日期与具体段落。`past_paper`：用户提供且能核实考试年份/级别/卷次的材料。`user_material`：教材、课程、错题、词表及未核实试卷。`original`：Agent 新写的辅助训练题。文件名含“真题”不足以升级来源等级；缺少年份或版本时留空。

`material add` 只处理学生指定的本地文件，支持 PDF、DOCX、TXT、MD 与常见音频。按 SHA-256 去重，记录原始路径、页码/行号/时间标记、类型、级别、题型和归类置信度。PDF 提取不到文字时提示扫描件限制；不要猜题干或答案。导入只建立材料索引，不自动把任意文本切成可训练题；用 `material item-add` 补一题，或将人工核对过的多题清单交给 `material batch-add --input FILE.json`。答案只在来源确证后标 `verified`。

批量清单使用 UTF-8 JSON，最多 500 题；任一条不合规则整批不写入。`material_id` 是已导入材料的 ID，也可在单条题目中覆盖。允许的题目字段为 `material_id`、`source_type`、`section`、`subtype`、`prompt`、`answer`、`answer_status`、`evidence`、`locator`、`audio_path`、`transcript`。原创题没有 `material_id` 时必须标 `source_type: "original"`；未核验答案可省略 `answer`，不要伪造。

```json
{
  "schema_version": 1,
  "material_id": "先由 material add 返回",
  "items": [
    {
      "section": "reading",
      "subtype": "careful_reading",
      "prompt": "题干和 A–D 选项",
      "answer": "B",
      "answer_status": "verified",
      "evidence": "原文第 2 页第 3 段",
      "locator": "page:2"
    }
  ]
}
```

清单是结构示例，不是题库。导入后先检查返回的 `added_item_ids` 和 `skipped_item_ids`；重复运行不会重复建题。

真题和商业材料不随 Skill 分发，也不自动从第三方仓库抓取。原创题不得冒充真题；题干、答案和解析都要独立创作。音频材料只读引用路径；未提供可播放音频时，不能调用 `drill --section listening` 产生一场假的听力测验。

## 题目质量门槛

- 客观题有唯一可解释的最佳答案；阅读题能指出段落或句子，错误选项能解释错在范围、主体、因果、态度、时间或过度推断。
- 作文与翻译只存题目/提示，不存所谓“唯一标准译文”。反馈要保留原意，区别信息遗漏、语言错误和风格建议。
- 同一材料重复导入不重复建题；材料归类低置信时不自动绑定具体题型。
