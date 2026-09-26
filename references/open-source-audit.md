# 开源项目蒸馏与采用边界

核验日期：2026-09-26。固定 commit、许可和采用方式见 [`sources.lock.json`](../sources.lock.json)。这里记录“具体吸收了什么”和“为何没有直接引入整套项目”；项目仍由本仓库独立维护。

| 项目 | 许可 | 对本 Skill 有用的机制 | 采用方式 |
|---|---|---|---|
| [Py-FSRS](https://github.com/open-spaced-repetition/py-fsrs) | MIT | 以历史提取评分安排到期复习 | 固定 `6.3.2` 版本，**直接调用**词汇卡调度；考前日期上限与本地固定间隔降级由本项目负责 |
| [cet-skill](https://github.com/Liuxiangjian-ai/cet-skill) | MIT | 四、六级难度区分、原创题、干扰项检查 | 只蒸馏任务质量问题，独立写出题协议和程序门槛 |
| [writing-assessment](https://github.com/daix74991-jpg/writing-assessment) | MIT | 先收原文证据再批改、非官方参考分标注 | 只吸收反馈顺序，不复制 rubric 或承诺官方分数 |
| [CET AI Coach](https://github.com/9-GETOVER-9/-cet-ai-coach) | MIT | 分题型错因与本地进度 | 独立实现记录与效率五维视图，不使用其线性分数估算或设备路径 |
| [Exam Clock](https://github.com/LUOLIN926/exam-clock) | MIT | 模拟考试的阶段时长与本地时间线 | 只借鉴“计时、阶段和暂停须分清”的设计；正式流程仍以当次官方通知为准 |
| [Anki](https://github.com/ankitects/anki) | AGPL-3.0-or-later | 到期复习队列与错题回访的成熟交互 | 对照产品机制；不复制代码、界面或牌组 |
| [LanguageTool](https://github.com/languagetool-org/languagetool) | LGPL-2.1 | 语法反馈需要具体位置和规则 | 对照反馈要求；不引入大型运行依赖，不把规则提示当作考试评分 |
| [English Multiple-Choice Practice Machine](https://github.com/mo9652962-ai/english-multiple-choice-practice-machine) | GPL-3.0 | 本地优先练题与错题回访 | 功能对照，无代码或题库复制 |

考研教练 Skill 提供了现实容量、主次矛盾、来源分级、自我调节与效率复盘的结构线索；本项目把这些线索改写为 CET 笔试的题型、计时与成绩边界。没有引入目标院校、专业课等不适用模型。
