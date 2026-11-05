# tests

`keel` 包的单元测试，每个模块一个 `test_<模块>.py`。在 `work/` 下运行 `python3 -m unittest discover -s tests -t .`。

已安装 `bd` 时，入口集成测试会使用真实 Beads 验证新建、重跑、默认模板迁移与克隆恢复；远端只使用临时的本地 Git 仓库。
