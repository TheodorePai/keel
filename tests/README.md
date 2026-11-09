# tests

`keel` 包的单元测试，每个模块一个 `test_<模块>.py`。在源码根目录（包含 `pyproject.toml` 的目录）运行 `python3 -m unittest discover -s tests -t .`。

已安装 `bd` 时，入口集成测试会使用真实 Beads 验证新建、重跑、默认模板迁移与克隆恢复；远端只使用临时的本地 Git 仓库。

`.github/workflows/ci.yml` 在 Windows、Linux、macOS 的 Python 3.11/3.12 上检查 Git 快照、工作模式入口和 runtime。快照回归覆盖中文及空格路径、`core.autocrlf=false/true`、暂存/历史检查，以及原 index 和工作区保持不变；入口回归保留 POSIX 文件名中反斜杠的语义。runtime 回归使用真实子进程验证进程身份、跨进程锁及异常退出后的解锁，并验证目录链接（Windows junction）、清理目标被替换、硬链接锁和原子写入失败时的保护。Linux 完整测试任务另行安装全部外部工具并拒绝跳过集成测试。

完整 GitHub 门禁与贡献方式见 [贡献说明](../CONTRIBUTING.md)。

`test_release.py` 验证每日发版的版本选择、积压 PR 检测、无更新跳过、发布来源与版本快照、安装包版本及摘要，以及已发布版本重试时不重复写入。Git 测试使用本地临时仓库，GitHub 写操作使用替身；真实云端发布由 `.github/workflows/release.yml` 验收。

Windows secrets 集成任务安装校验 SHA-256 的原生 fnox 1.46.0 与 age 1.3.2，运行整个 secrets 测试模块并拒绝跳过。测试使用隔离配置目录和模拟值，覆盖中文/空格/非 BMP 路径、真实环境注入、轮换、导入冲突，以及 Windows ACL 放宽后的诊断。跨平台任务另行运行不依赖外部工具的配置与权限回归；POSIX 保留 0600 要求。

编码回归同时模拟 CP1252/GBK 默认文件编码，并在 Windows Python 3.11/3.12 中关闭 UTF-8 模式，验证配置创建后的读取、说明更新、隐藏共享项、项目引用、注册索引、诊断和真实 fnox 中文值往返。仅设置日志编码不改变文件编码；该任务断言 utf8_mode 为 0，并拒绝跳过。
跨平台回归还覆盖本地 Dolt remote 的 file URI 编码、报告的 protocol 引用、入口链接诊断，以及 verify 的失败阻断、工作目录和暂存检查。verify 测试使用同一 Python 解释器执行契约脚本，项目的 verify 命令仍交由本机默认 shell 执行。

Windows Beads 集成任务安装校验 SHA-256 的原生 bd 1.3.2，验收初始化、模板迁移、发布恢复、worktree 数据库和并发认领，拒绝跳过。Dolt 嵌套 Git 缓存路径较长，测试进程通过 GIT_CONFIG_COUNT 向子进程启用 core.longpaths，不修改用户或机器的 Git 配置。
