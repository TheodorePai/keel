# Keel

**面向多项目 Agent 协作的工程工作流。**

[![CI](https://github.com/omoyx/keel/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/omoyx/keel/actions/workflows/ci.yml) [![Release](https://img.shields.io/github/v/release/omoyx/keel)](https://github.com/omoyx/keel/releases/latest) [![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Keel 用一套命令组织项目知识、任务、大文件、密钥和共享 skill，让开发者在切换项目、Agent 或机器时，能够沿用已有约定、恢复工作上下文，并在提交前检查项目结构。

[快速开始](#快速开始) · [使用指南](USAGE.md) · [版本记录](https://github.com/omoyx/keel/releases) · [参与贡献](CONTRIBUTING.md)

## 为什么使用 Keel

当多个项目各自维护 Agent 规则、任务记录和环境配置时，同一套流程容易重复编写，项目知识也容易散落在历史对话里。Keel 将这些约定整理为随项目保存、可以检查和更新的文件：

- **知识有归属**：为架构说明、设计决策、研究和实测报告提供不同模板，检查结构、引用和定稿约束。
- **任务可接续**：接入 [Beads](https://github.com/steveyegge/beads)，用任务与依赖记录进展；通过 `keel status` 查看进行中、阻塞项和里程碑。
- **资产可恢复**：大文件通过清单登记，按存储位置取回并校验摘要；运行产物有独立的生命周期管理。
- **密钥按需使用**：基于 [fnox](https://github.com/jdx/fnox) 与 [age](https://github.com/FiloSottile/age) 保存共享密钥，项目只声明所需名称，运行时注入。
- **约定可复用**：分发共享 skill，维护 Agent 入口，并将结构检查接入 Git 提交钩子。

适合希望在多个代码仓库中复用协作流程的开发者。日常任务操作直接使用 `bd`；项目中的 `docs/` 也可以作为 Obsidian 笔记查看。

## 快速开始

### 1. 安装

需要 Python 3.11+、[uv](https://docs.astral.sh/uv/getting-started/installation/)、Git 和 [Beads CLI（`bd`）](https://github.com/steveyegge/beads)。Keel 的 Python 包仅使用标准库；GitHub、大文件和密钥功能的外部工具见[依赖说明](USAGE.md#依赖)。

从已发布的版本安装：

```bash
uv tool install git+https://github.com/omoyx/keel.git@v0.5.5
keel --version
```

其他版本见 [Releases](https://github.com/omoyx/keel/releases)。

### 2. 接入项目

```bash
mkdir my-project
cd my-project
keel init
```

`keel init` 初始化 Git 和 Beads，生成项目配置、Agent 入口、共享 skill 与提交钩子，并只提交它管理的文件。已有项目也可以在根目录运行；原有项目规则会保留，冲突会提示处理。

接入后，项目按以下职责组织：

```text
my-project/
├── AGENTS.md        # 项目职责、边界与 Agent 能力入口
├── keel.toml        # Keel 配置
├── .agents/skills/  # 共享与项目专用 skill
├── .beads/          # 任务、依赖与进度
├── docs/            # 项目知识与证据
└── work/            # 源码、数据与正式交付物
```

需要同时创建 GitHub 私有仓库时，使用 `keel init --github`；它会推送代码和任务库，需要先安装并登录 `gh`。只准备文件、不初始化任务库和提交时，使用 `keel init --no-commit`。

### 3. 开始使用

```bash
bd ready                           # 找到可以开始的任务
keel new reference "项目架构"       # 新建架构说明，按模板填写
keel check                         # 检查当前项目
```

`keel check` 会检查笔记结构与链接、Agent 入口、共享 skill、项目布局，以及进入 Git 的大文件、明文密钥文件和疑似密钥。提交前还会检查实际暂存的内容。

## 常用命令

| 要做什么 | 命令 |
|---|---|
| 新建或恢复项目 | `keel init [--github]` |
| 查看笔记类型、新建笔记 | `keel types` · `keel new <类型> <标题>` |
| 检查项目或暂存内容 | `keel check` · `keel check --staged` |
| 查看任务与里程碑 | `keel status` |
| 登记、取回与校验大文件 | `keel asset` · `keel fetch` |
| 声明与管理共享密钥 | `keel secret` |
| 更新或查看共享 skill | `keel sync` · `keel skills` |
| 查看或清理运行产物 | `keel local status` · `keel local clean` |
| 排查工具、账号和存储问题 | `keel doctor` |

各命令的参数见 `keel <命令> --help`；任务认领、更新和关闭直接使用 `bd`。

## 文档与集成

- [使用指南](USAGE.md)：依赖、升级、换机恢复、Agent 入口及兼容处理。
- [默认工作模式](keel/data/skills/keel/references/work-mode.md)：项目规则、skill 与工具如何分工。
- [知识收尾](keel/data/skills/keel/references/knowledge-upkeep.md)：让已验证的结论留在可继续使用的位置。
- [运行产物管理](keel/data/skills/keel/references/runtime-files.md)：运行记录、保留和清理规则。

Beads 的 skill 与 Claude Code、Codex、Cursor 上下文钩子由 `bd setup` 安装，项目入口由 Keel 维护。Obsidian 可直接打开项目根目录作为库；可选的 [moi 工作台](moi/README.md) 为试用组件，需单独接入。

## 获取帮助与参与贡献

遇到问题可先运行 `keel doctor`，再到 [GitHub Issues](https://github.com/omoyx/keel/issues) 提供复现步骤、预期与实际行为，以及操作系统、Python 和 Keel 版本。

欢迎提交改进建议和 PR。开发环境、测试要求及 CI 检查见[贡献说明](CONTRIBUTING.md)。

源码导航：

- [keel](keel/README.md)：Python 命令行工具与随包分发的模板、配置和 skill。
- [tests](tests/README.md)：单元测试与集成回归。
- [moi](moi/README.md)：可选的本地工作台适配器。

## 许可证

Keel 采用 [MIT License](LICENSE)。内置第三方 Obsidian 插件和主题遵循各自许可证，见 [THIRD_PARTY.md](THIRD_PARTY.md)。

本项目与 Kubernetes 自动部署工具 [keel.sh](https://keel.sh/) 无关。
