# Keel 使用指南

Keel 的安装、升级、换机恢复与 Agent 接入说明。首次使用见 [README 的快速开始](README.md#快速开始)。

## 依赖

| 使用场景 | 所需工具 |
|---|---|
| 安装与运行 Keel | Python 3.11+、[uv](https://docs.astral.sh/uv/getting-started/installation/)；uv 可按需准备 Python |
| 完整初始化项目与任务库 | Git、[Beads CLI（`bd`）](https://github.com/steveyegge/beads) |
| 创建 GitHub 仓库、使用 GitHub 资产存储 | [GitHub CLI（`gh`）](https://cli.github.com/)，并运行 `gh auth login` 登录 |
| 使用 Hugging Face 资产存储 | [Hugging Face CLI（`hf`）](https://huggingface.co/docs/huggingface_hub/guides/cli)，可通过 `uv tool install huggingface_hub` 安装 |
| 保存与注入共享密钥 | [fnox](https://github.com/jdx/fnox)、[age](https://github.com/FiloSottile/age) |

Obsidian 和 moi 是可选界面；命令行使用不依赖它们。`keel doctor` 检查当前项目所需的工具、账号、存储和资产是否就绪。

## 安装与升级

每台机器只需安装一次。安装已发布版本：

```bash
uv tool install git+https://github.com/omoyx/keel.git@v0.5.5
```

升级时，从 [Releases](https://github.com/omoyx/keel/releases) 选择目标标签，替换命令中的版本，并加上 `--force`：

```bash
uv tool install --force git+https://github.com/omoyx/keel.git@v0.5.5
```

随后在每个接入项目的根目录运行：

```bash
keel init
keel check
```

`keel init` 会更新共享 skill、迁移已知旧入口并启用提交钩子。也可单独运行 `keel sync` 更新共享 skill，再运行 `keel init` 补齐项目入口。

## 初始化与换机恢复

新项目在空目录运行 `keel init`；已有项目在项目根目录执行同一命令。默认初始化任务库并提交 Keel 管理的文件，不夹带已有的其他改动。

- `keel init --no-commit`：只准备文件，不初始化任务库、不提交；仍写入默认工作约定。
- `keel init --github`：额外创建 GitHub 私有仓库，并推送代码与任务库。

在另一台机器上，先安装 Keel 及所需工具，再克隆项目：

```bash
git clone https://github.com/YOUR-ACCOUNT/my-project.git
cd my-project
keel init
keel fetch
```

将克隆地址替换为自己的项目。`keel init` 取回任务库、启用钩子并生成本机 Obsidian 模板；`keel fetch` 在需要时按资产清单取回并校验大文件。使用共享密钥的项目还需要在新机器上配置 fnox 与 age；Git 克隆不会取回密钥值或 age 私钥。

密钥功能需要本机安装 `fnox` 和 age 提供的 `age-keygen`，并加入 PATH；支持 Windows 原生命令。新建 age 私钥时，POSIX 使用 `0600` 权限，Windows 使用仅允许当前用户和 SYSTEM 的受保护 ACL；`keel doctor` 按各平台规则检查。已有 age 配置和私钥不会被重新生成，诊断发现权限问题时先核查已有文件。

在 Obsidian 中使用“打开文件夹作为库”选择项目根目录，可查看任务面板和笔记视图。

## Agent 入口与工作约定

项目通过已安装的 `keel` 命令运行，不依赖 Keel 源码目录。`keel init` 装入共享 skill，在 `AGENTS.md` 中补齐简短入口；项目自己的职责、边界和必要调用条件仍由该文件维护。例如：

```text
本项目使用 Keel 工作流（命令 keel，说明见 .agents/skills/keel/SKILL.md；不是 keel.sh）。
```

项目根目录的 `CLAUDE.md` 只保留一行 `@AGENTS.md`，统一引用项目规则。`AGENTS.md` 同时引用 `.agents/skills/beads/SKILL.md`；任务流程由该 skill 和 `bd prime` 提供。Beads 的 skill、Claude Code/Codex/Cursor 上下文钩子由 `bd setup` 安装，入口说明由 Keel 维护。

默认工作约定包括：

- 完成可验证里程碑且项目检查通过后，主动创建本地 Git 提交，只包含当前任务的改动。
- Git 推送与 Dolt 远端同步沿用用户、上层指令或项目规则的已有授权；未约定目标或范围时再确认，已有授权无需重复确认。用户或项目的额外限制仍须遵守。
- 按[默认工作模式](keel/data/skills/keel/references/work-mode.md)组织项目：常驻约定保留职责、边界和重要能力的调用条件；流程放项目 skill，资料按需读取，确定性工作交给工具。
- 将经实际验证、值得复用的流程与常见错误处理方法补充到已有 skill，没有合适位置时再创建项目 skill；能用代码、测试或检查防止复发的，同时落实。
- 按[知识收尾](keel/data/skills/keel/references/knowledge-upkeep.md)保存后续还要使用的事实，任务、计划与进度留在 Beads。

## 入口迁移与冲突处理

重复运行 `keel init` 会补齐缺失入口，更新独立的已知旧默认授权条款，保留项目自定义规则、代码示例和注释，不叠加新旧默认条款。

初始化会自动迁移已核实的 Beads 1.3.2 默认模板。未知或经过修改的生成块，以及 `CLAUDE.md` 中的额外规则，会在初始化前被拦截，保留原文并提示手动迁移。

`keel check` 和提交前的暂存检查会拦截缺失的必需入口、`CLAUDE.md` 中的额外内容及不存在的导入目标。启用 `keel` skill 后，默认工作模式入口及其本地 Markdown 链接也必须有效。

检查器验证入口可达性与结构；Agent 能力是否在正确场景触发，仍需按风险用实际场景验收。

## 进一步阅读

- [共享 Keel skill](keel/data/skills/keel/SKILL.md)：任务与笔记分工、文件位置、资产、密钥和共享 skill 的日常操作。
- [运行产物管理](keel/data/skills/keel/references/runtime-files.md)：`work/.local/` 的记录、保留与清理。
- [贡献说明](CONTRIBUTING.md)：源码测试与 CI 要求。
- [返回项目首页](README.md)。
