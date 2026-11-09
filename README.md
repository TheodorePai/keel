# Keel

跨项目的 Agent 工作流底座：笔记结构、任务（Beads）、大文件、密钥和共享 skill 用同一套命令管理。与 Kubernetes 的 keel.sh 无关。

## 安装

每台机器装一次。需要 Python 3.11+（uv 会自动准备）、`gh` 和 `bd`；大文件功能另需 `hf`（`uv tool install huggingface_hub`）。

```bash
uv tool install keel --from git+https://github.com/omoyx/keel.git@v<版本>
```

升级：把版本标签换成新的，加 `--force` 再执行一次，然后在各项目里运行 `keel init` 更新钩子。

## 新项目

```bash
mkdir my-app && cd my-app
keel init --github
```

依次完成 `git init`、`bd init`、写入规则与配置、装入共享 skill，并且只提交它管理的文件。Beads 的 skill 和 Claude/Codex/Cursor 上下文钩子由 `bd setup` 安装，入口说明由 Keel 统一维护。加 `--github` 时，还会另建一个私有仓库，推送代码和任务库。

在 Obsidian 里用“打开文件夹作为库”选择项目根目录，就能看到任务面板和笔记视图。

## 另一台机器

```bash
git clone https://github.com/<你>/my-app.git && cd my-app
keel init     # 取回任务库，启用钩子，生成本机 Obsidian 模板
keel fetch    # 需要时取回大文件
```

## 命令

| 命令 | 用途 |
|---|---|
| `keel init [--github]` | 新建项目，或在新机器上恢复项目 |
| `keel new` / `types` / `check` | 按类型写笔记，提交前检查结构 |
| `keel status` | 查看进行中、阻塞的任务和里程碑 |
| `keel local status` / `clean` | 查看运行产物大小，预览或显式清理指定路径 |
| `keel asset` / `fetch` | 登记与取回大文件 |
| `keel secret` | 声明密钥，运行时注入（值存放在 fnox） |
| `keel sync` / `skills` | 装入共享 skill |
| `keel doctor` | 检查工具、账号、存储 |

每条命令的细节见 `keel <命令> --help`。日常任务直接用 `bd`（`bd ready`、`bd update <id> --claim`、`bd close <id>`），Keel 不代理。

## 接入约定

项目不依赖 Keel 源码：`keel init` 装入 `keel` skill，并在 `AGENTS.md` 补齐 skill 与默认工作模式的简短入口。项目自己的职责、边界及必要调用条件仍写在 `AGENTS.md`；Keel 使用入口例如：

```text
本项目使用 Keel 工作流（命令 keel，说明见 .agents/skills/keel/SKILL.md；不是 keel.sh）。
```

项目根目录的 `CLAUDE.md` 只保留一行 `@AGENTS.md`，项目规则统一写在 `AGENTS.md`，避免重复和冲突。
`AGENTS.md` 同时简短引用 `.agents/skills/beads/SKILL.md`；完整任务流程由该 skill 和 `bd prime` 提供。默认在完成可验证里程碑且项目检查通过后，主动进行本地 Git 提交，只包含当前任务的改动。Git 推送和 Dolt 远端同步沿用用户、上层指令或项目规则的已有授权；未约定目标或范围时再确认，已有授权无需重复确认。用户或项目的额外限制仍须遵守。
重复运行 `keel init` 会原位更新独立的已知旧默认授权条款，保留项目自定义规则、代码示例和注释；不会把新旧默认条款叠加。即使使用 `--no-commit` 仅准备文件，也会写入该默认约定。
`keel init` 补齐缺失的入口，并自动迁移已核实的 Beads 1.3.2 默认模板，保留 `AGENTS.md` 中的项目规则。未知或改过的生成块、`CLAUDE.md` 的额外规则会在初始化前被拦截，保留原文并提示手动迁移。
`keel check` 和提交前的暂存检查会拦截缺失入口、额外内容及不存在的导入目标。

新项目默认采用[工作模式](keel/data/skills/keel/references/work-mode.md)：常驻约定保留职责、边界和重要能力的调用条件；流程放项目 skill，资料按需读取，确定性工作交给工具。入口与共享说明随 Keel 版本分发，检查器验证可达性与结构；能力是否正确触发仍需按风险用实际场景验收。
已有项目升级后运行 `keel init` 补齐工作模式入口及 skill 提炼约定，并保留原有规则。经实际验证、值得复用的操作流程和常见错误处理方法应补充已有或创建项目 skill；能用代码、测试或检查防止复发的，同时落实。启用 `keel` skill 却删除工作模式入口，或入口中的本地 Markdown 链接失效时，`keel check` 会报错。

## 源码

- [keel](keel/README.md)：`keel` 命令的 Python 包，连同随包分发的模板、笔记类型和共享 skill
- [tests](tests/README.md)：单元测试，`python3 -m unittest discover -s tests -t .`
- [moi](moi/README.md)：可选的本地人用工作台，读取项目情况并提供固定快捷操作

## 贡献

提交问题、PR 和 CI 检查的范围见 [贡献说明](CONTRIBUTING.md)。

## 许可证

[MIT](LICENSE)。内置的第三方 Obsidian 插件和主题遵循各自的许可证，见 [THIRD_PARTY.md](THIRD_PARTY.md)。
