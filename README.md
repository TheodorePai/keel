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

依次完成 `git init`、`bd init`、写入规则与配置、装入共享 skill，并且只提交它管理的文件。加 `--github` 时，还会另建一个私有仓库，推送代码和任务库。

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
| `keel asset` / `fetch` | 登记与取回大文件 |
| `keel secret` | 声明密钥，运行时注入（值存放在 fnox） |
| `keel sync` / `skills` | 装入共享 skill |
| `keel doctor` | 检查工具、账号、存储 |

每条命令的细节见 `keel <命令> --help`。日常任务直接用 `bd`（`bd ready`、`bd update <id> --claim`、`bd close <id>`），Keel 不代理。

## 接入约定

项目不依赖 Keel 源码：`keel init` 装入 `keel` skill，Agent 从 `.agents/skills/keel/SKILL.md` 读到规则。项目的 `AGENTS.md` 只需一行：

```text
本项目使用 Keel 工作流（命令 keel，说明见 .agents/skills/keel/SKILL.md；不是 keel.sh）。
```

## 源码

- [keel](keel/README.md)：`keel` 命令的 Python 包，连同随包分发的模板、笔记类型和共享 skill
- [tests](tests/README.md)：单元测试，`python3 -m unittest discover -s tests -t .`

## 许可证

[MIT](LICENSE)。内置的第三方 Obsidian 插件和主题遵循各自的许可证，见 [THIRD_PARTY.md](THIRD_PARTY.md)。
