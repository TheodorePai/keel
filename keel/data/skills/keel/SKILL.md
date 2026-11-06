---
name: keel
description: 在 Keel 项目中接入或恢复工作环境、维护 Agent 入口或 skill、查询整体任务进度，或处理笔记、资产、项目密钥及结构检查时使用。
---

# Keel 工作流

项目根目录有 `keel.toml` 即已接入。开始项目工作时运行 `keel local sweep --daily`，同一天重复调用会跳过；它只回收有明确生命周期且已到期的可丢弃产物，其他内容只提示。按当前任务读取相关部分；结构规则由 `keel check` 执行（pre-commit 已接入），不要绕过。

接入项目、维护 `AGENTS.md` 或创建/调整项目 skill 时，读取[工作模式](references/work-mode.md)，统一职责边界、按需调用和能力验收。已有明确任务与位置时直接开展工作。

## 任务和笔记的分工

- 任务、计划、进度、认领只进 Beads（`bd ready`、`bd update <id> --claim`、`bd close <id>`），不要写成笔记或 TODO 文件。
- 任务字段各管一件事：`--description` 写为什么做、做什么；`--acceptance` 逐条写可核验的完成条件；进展与交接用 `--append-notes` 追加，不覆盖历史。超过两三句就用 Markdown 小标题和列表分段，简单任务一句话即可。
- 笔记记录知识和证据，用属性 `beads: [<id>]` 关联任务。
- 查询项目整体任务进度时，运行 `keel status`（进行中、阻塞、里程碑、最近的记录和标签），再按需 `bd show <id>` 展开；具体运行、测试或设备状态直接查对应来源。

## 知识收尾

工作验证或改变了以后还要用的事实（环境与网络状态、接口、部署方式、排障结论、项目入口）时，结束任务前把它落到长期位置，并按[知识收尾](references/knowledge-upkeep.md)核对。普通问答和没有新事实的改动无需新建文档。

## 里程碑

里程碑就是 bd epic（`bd create --type=epic`，子任务用 `--parent=<epic>`），达成时 `bd close <epic> --reason "<一句话：现在能做到什么>"`。不写里程碑笔记。

由你判断要不要建，不是每个任务都带。只在以下情况建：
- 用户提出一个需要多个任务才能完成的目标；
- 一次发布；
- 一个能独立演示或验收的能力。

单个修复、重构、文档改动不建；拿不准就不建，用户要求时再补。关闭原因写成果，不写过程。

## 选笔记类型：先问两个问题

1. 写的是“现在为真”的东西（就地更新），还是“某个时点发生”的事（带日期，定稿后只追加）？
2. 具体写什么：

| 类型 | 回答 |
|---|---|
| reference | 系统现在是什么：架构、接口契约、数据 schema |
| concept | 为什么这样设计：理念、原理 |
| skill | 怎么做：可重复的步骤，放 `.agents/skills/<name>/SKILL.md` |
| decision | 选了什么、为什么、放弃了什么 |
| protocol | 测量前冻结的输入、样本、指标、门槛 |
| report | 按方案测了，观察到什么 |
| incident | 计划外失败：现象、原因、修复、验证 |
| research | 外部知识：来源、核实日期、结论 |
| idea | 自己的判断、灵感，未验证 |

`keel types` 显示各类型所在目录。

## 常用命令

```bash
keel new decision "标题" --beads <id>          # 决策自动编号
keel new incident "标题" --cause <原因标签>      # 同一原因第 3 次会提醒提炼成规则
keel new report "标题" --protocol <protocol 文件名>
keel check                                     # 提交前必须通过
keel check --staged                            # 核对实际暂存内容，pre-commit 自动使用
```

- 先冻结并提交 protocol（`status: frozen`），再写引用它的 report。
- 状态到终态（decision accepted、report final、protocol frozen）后只能在末尾追加，不能改写已有内容。
- 每种类型有少量必需的二级标题（见模板）；其余正文自由写。

## 文件放哪

先按内容的作用归位，“长期有用”本身不决定位置：

| 内容 | 位置 |
|---|---|
| 任务状态、计划、进度、交接 | Beads |
| 知识：当前事实、设计理由、操作步骤、实测结论与证据 | 笔记（`docs/`）或 skill；证据放笔记附件 |
| 项目资产：运行或交付所需的代码、配置、数据、模型、交付物 | `work/` |
| 允许丢失的运行产物：run、cache、调试输出、临时回执 | `work/.local/` |

拿不准时问：**它是项目运行或交付的组成部分吗？**是就放 `work/`；是解释、记录或证明，就写成笔记。

- 根目录只放 `AGENTS.md`、`README.md`、`keel.toml` 等 Keel 文件、`docs/`、`work/` 和点开头的工具目录；`package.json`、`pyproject.toml`、`Makefile` 也放 `work/`，命令在 `work/` 下执行。
- `work/` 的每个一级目录要有 `README.md`，首段一句话说清装什么；同时在 `work/README.md` 地图里加一行链接。新建一级目录前先看地图，能归入已有目录就不新建。
- 一级目录用名词说明装的是什么：要么都是一个产品的组成部分（`app/`、`data/`、`tests/`），要么都是独立子项目（每个 Demo 一个目录，报告、脚本、测试跟着它）。
- 不用 `tmp`、`output`、`misc`、`old`、`backup`、`archive`、`final`、`_v2`、日期后缀这类名字；历史版本归 Git，退役代码直接删。
- 主动生成且允许丢失的运行产物（run、cache、调试输出）写 `work/.local/`；需要保留的成果归入 `work/` 所属目录，证据归入笔记附件。工具管理的依赖和缓存可保留默认位置并忽略。任务隔离、缓存复用和清理见[运行产物](references/runtime-files.md)；产生运行产物时按该说明记录生命周期，运行结束及任务收尾时转存成果、确认服务停止并声明可丢弃或保留，再清理本任务已符合条件的目录。
- 第二层以下由该目录 README 说明，`keel check` 不管；领域契约（数据指纹、清单格式等）写成脚本，在 `keel.toml` 声明 `verify = "<命令>"`，`keel check` 和提交前会在 `work/` 下运行它，非零退出即失败。

## 附件与大文件

- 笔记附件放在笔记同目录的 `assets/`，文件名带笔记日期前缀，用相对路径的标准 Markdown 链接。
- 超过 5 MB 的文件不进 git。按“丢了能否找回”登记：

```bash
keel asset add <路径> --kind dataset      # 也可 model / deliverable / public / generated / local
keel fetch                                # 新机器按 assets.toml 取回并校验
keel asset verify <名称>                  # 列出改动、缺少、多出的文件
```

一个数据集目录是一条资产，不要逐个文件登记；单目录超过 1 万个文件先合并成 Parquet / WebDataset 分片。

## 密钥

- 不要写 `.env`，也不要把密钥写进代码或笔记。密钥统一存在本机的 fnox 全局配置，项目只声明变量名。
- 声明：`keel secret use OPENAI_API_KEY`（写入 keel.toml 和 fnox.toml）。查看：`keel secret list`（不显示值）。
- 本机有哪些共享密钥、谁在用：`keel secret list --all`（只显示名称、说明和引用的项目）；写说明：`keel secret set <名称> --note "..."`。
- 变量名和共享密钥名不同时用别名：`keel secret use OPENAI_API_KEY=DASHSCOPE_API_KEY`（OpenAI 兼容接口常见）。
- 运行需要密钥的命令：`fnox exec -- <命令>`。只有声明过的变量会注入。
- 本机缺某个密钥时请用户提供，由用户运行 `pbpaste | keel secret set <名称>`；不要索要或打印密钥值。
- `keel check` 会拦截进入 git 的 `.env` 和疑似密钥。

## 共享 skill

- `keel.toml` 的 `skills = [...]` 列出从 Keel 装入的 skill，`keel sync` 复制进 `.agents/skills/` 并生成 `.claude/skills/` 镜像。
- 不要直接改装入的共享 skill；需要改就改 Keel 源码并发布新版本，各项目再 `keel sync`。项目独有的 skill 直接写在 `.agents/skills/`。
- 不使用软链接。

## 出问题时

`keel doctor` 检查工具、账号、存储和资产是否就绪，并给出补救命令。
