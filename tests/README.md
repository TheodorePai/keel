# tests

`keel` 包的单元测试，每个模块一个 `test_<模块>.py`。在本仓库根目录运行 `python3 -m unittest discover -s tests -t .`。

已安装 `bd` 时，入口集成测试会使用真实 Beads 验证新建、重跑、默认模板迁移与克隆恢复；远端只使用临时的本地 Git 仓库。

`.github/workflows/path-portability.yml` 在 Windows、Linux、macOS 的 Python 3.11/3.12 上检查 Git 快照和工作模式入口。快照回归覆盖中文及空格路径、`core.autocrlf=false/true`、暂存/历史检查，以及原 index 和工作区保持不变；入口回归保留 POSIX 文件名中反斜杠的语义。该工作流只验收这两项修复，不能替代完整测试，也不代表未安装外部工具时跳过的集成测试已经通过。
