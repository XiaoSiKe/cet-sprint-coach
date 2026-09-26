# CET Sprint Coach · 四六级英语考试冲刺 Skill

面向大学英语四级、六级**笔试**的 Codex Skill。学生用自然语言提出目标、提交材料和作答；Skill 在后台维护本地学习档案，按真实练习结果安排下一步。口试只提供官方信息核验，不与笔试成绩混算。

## 能做什么

- 根据考试日期、现实时间和最近练习，安排一个周主攻点与每日最多三个任务。
- 导入个人 PDF、DOCX、文本与音频，保留来源和位置；通过结构化 JSON 一次导入多道已整理题目。
- 一次只练一题；答案与听力脚本在学生作答前不显示。到期复习可按题目 ID 精确取题。
- 分开记录模考原始正确率和官方报道分；写作、翻译只给有作品证据的教练反馈。
- 用 [Py-FSRS](https://github.com/open-spaced-repetition/py-fsrs) 安排词汇复习；依赖缺失时明确降级为固定间隔。

## 安装到 Codex

需要 Python 3.12+ 和 `uv`。把仓库克隆到 Codex Skill 目录，再安装锁定的依赖：

```bash
git clone https://github.com/XiaoSiKe/cet-sprint-coach.git ~/.codex/skills/cet-sprint-coach
uv sync --project ~/.codex/skills/cet-sprint-coach --python 3.12 --frozen
```

之后在 Codex 中说“用 `$cet-sprint-coach` 帮我准备四级/六级”。首次接触只需确认级别；缺少成绩时会先安排短时诊断。学习数据写入你选择的工作区 `.cet-sprint/`，不写进 Skill 仓库。原材料只读，不自动上传。

## 本地引擎示例

以下命令主要供 Agent 调用，学生通常无需手动运行：

```bash
SKILL_ROOT="$HOME/.codex/skills/cet-sprint-coach"
WORKSPACE="$HOME/Documents/my-cet-study"
uv run --project "$SKILL_ROOT" --frozen python "$SKILL_ROOT/scripts/cet.py" --workspace "$WORKSPACE" init --level 4
uv run --project "$SKILL_ROOT" --frozen python "$SKILL_ROOT/scripts/cet.py" --workspace "$WORKSPACE" today
uv run --project "$SKILL_ROOT" --frozen python "$SKILL_ROOT/scripts/cet.py" --workspace "$WORKSPACE" material batch-add --input /path/to/checked-items.json
uv run --project "$SKILL_ROOT" --frozen python "$SKILL_ROOT/scripts/cet.py" --workspace "$WORKSPACE" review
uv run --project "$SKILL_ROOT" --frozen python "$SKILL_ROOT/scripts/cet.py" --workspace "$WORKSPACE" drill --item-id ITEM_ID
```

批量清单格式与来源等级见 [材料规则](references/materials.md)。`material add` 仅建立文件索引；题目需经过人工核对后由 `item-add` 或 `batch-add` 入库。真题与商业题库不随仓库分发。

## 成绩与验证

四六级官方报道分采用常模转换，不设及格线；本项目不把练习正确率线性换算成 710 分，也不承诺考试结果。考试安排与规则以[中国教育考试网](https://cet.neea.edu.cn/)和所在学校当期通知为准。

```bash
uv run --frozen python -m unittest discover -s tests -v
uvx --from ruff==0.16.9 ruff check cet_sprint scripts tests
uvx --from ruff==0.16.9 ruff format --check cet_sprint scripts tests
uv run --frozen python scripts/validate_skill.py
```

项目按 MIT 许可发布；参考项目、固定版本与许可边界见 [第三方来源](THIRD_PARTY_NOTICES.md) 和 [来源锁定记录](sources.lock.json)。
