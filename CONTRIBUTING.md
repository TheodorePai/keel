# 贡献

欢迎提交问题和 PR。请说明可复现的触发条件、预期行为、实际结果，以及 Python、Git 和操作系统版本。

贡献 PR 在评审与必需检查通过后以 squash 方式合入 `main`，GitHub 状态为 Merged。维护者同时将改动同步到开发树，后续版本从开发树发布源码快照。合并 PR 不等于发布版本；正式版本以标签和 Release 为准。

## 本地验证

在包含 `pyproject.toml` 的源码根目录运行：

```bash
python3 -m unittest discover -s tests -t .
uv build
```

完整测试需要 `bd`、`fnox` 和 `age`。缺少外部工具时，测试会明确跳过对应集成场景；请在 PR 中注明，不要将其视为完整集成验收。

## GitHub 检查

CI 对每个 PR 执行检查，不按改动路径跳过整个工作流：

- Windows、Linux、macOS × Python 3.11/3.12：Git 快照与入口路径回归。
- Linux × Python 3.11/3.12/3.14：完整测试，安装固定版本且校验 SHA-256 的外部工具，任何跳过都会使检查失败。
- 构建 sdist 和 wheel，在源码目录外安装 wheel 并检查命令及随包资源。
- `CI gate`：仅在以上检查全部成功时通过，失败、取消或跳过都不能通过。

工作流只读仓库内容，不向贡献分支提供密钥，也不使用 `pull_request_target` 执行 PR 代码。第三方 Actions 固定到完整提交 SHA，Dependabot 定期提出更新。构建产物仅作为短期检查附件，不自动发布。
