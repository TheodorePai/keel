# tests

`keel` 包的单元测试，每个模块一个 `test_<模块>.py`。在源码根目录（包含 `pyproject.toml` 的目录）运行 `python3 -m unittest discover -s tests -t .`。

已安装 `bd` 时，入口集成测试会使用真实 Beads 验证新建、重跑、默认模板迁移与克隆恢复；远端只使用临时的本地 Git 仓库。

`.github/workflows/ci.yml` 在 Windows、Linux、macOS 的 Python 3.11/3.12 上检查 Git 快照和工作模式入口。快照回归覆盖中文及空格路径、`core.autocrlf=false/true`、暂存/历史检查，以及原 index 和工作区保持不变；入口回归保留 POSIX 文件名中反斜杠的语义。其中跨平台任务只验收这两项修复；Linux 完整测试任务另行安装全部外部工具并拒绝跳过集成测试。

完整 GitHub 门禁与贡献方式见 [贡献说明](../CONTRIBUTING.md)。

`test_release.py` 验证每日发版的版本选择、积压 PR 检测、无更新跳过、发布来源与版本快照、安装包版本及摘要，以及已发布版本重试时不重复写入。Git 测试使用本地临时仓库，GitHub 写操作使用替身；真实云端发布由 `.github/workflows/release.yml` 验收。
