# moi

可选的 moi 人用界面适配器：读取 Beads 和项目资料，提供固定快捷操作，不保存第二份任务状态。

这是试用组件，不随 `keel init` 安装，也不会打包进 Keel Python wheel。需要已安装的 Keel、Beads、moi 和 Bun。调用已安装 Keel 的 Python API，不导入本仓库的 `work/keel/`。

## 接入与更新

在接入项目根目录执行 `moi init --harness=codex` 后，运行 `python3 work/moi/setup.py` 将本目录的 applet 复制进本机 `.moi/`，再运行 `moi bundle`。已有工作区只需重新运行 setup 和 bundle；脚本只覆盖自己管理且未手工修改的文件。`.moi/` 是生成物，不作为源码修改位置。

`moi init` 可能自动安装依赖，运行前应先检查当前版本的行为和用户的下载约束。本目录不执行安装、升级或启动服务。依赖版本写在 `package.json`，React 和 Tailwind 由 moi 的运行环境使用。缺少依赖时按用户指定的可验证镜像安装，核对最终 tarball 和重定向后再下载。

首次编译 Widget 后，在 Widgets → Edit widgets → Hidden widgets 中点击加号加入概览，再点击 Done。等待卡片加载后，通过“打开项目首页”进入 View。后续更新通常只需 setup、bundle 和 refresh，不要直接操作 moi 内部布局文件。

## 分工

- `bridge.py`：通过已安装 Keel 的环境读取资料与运行检查，通过只读 Beads 查询读取任务。服务端输入和路径在这里验证。
- `home.tsx` / `home.server.ts`：项目首页及固定服务端入口。30 秒刷新，重新激活页面时刷新，失败保留旧数据。
- `overview.tsx`：概览 Widget，进入首页；不另建数据逻辑。
- `setup.py`：将上述文件安装到 `.moi/`；冲突时停止，不覆盖手工修改。

服务端仅接受 `overview`、`machine`、`task`、`note`、`check`、`local`、`preview`、`doctor` 八种操作，无任意命令入口。`check` 直接调用安装包的 `check()`，不经过带自动回收的 CLI 主入口；`preview` 只调用 `clean(apply=False)`。没有删除、任务状态修改、密钥读写、发布和同步入口。

首页只展示终态报告和已关闭里程碑。待决定事项来自 Beads 的 `human` 标签，阻塞另列；来源不可用时显示未知，不解释成零。资料来源允许读取 `docs/` 内的 Markdown，拒绝越界与符号链接逃逸。Agent 按钮由用户点击发起，自动刷新不会调用模型。

本机使用情况独立读取和刷新，合并当前项目、Keel 已有登记与 macOS 文件索引发现的 `keel.toml`，去重后最多展示 24 个项目；其他系统或索引不可用时保留已知项目并提示范围。登记本来只用于密钥声明项目，因此不能单独代表完整接入清单；未索引项目仍可能遗漏。每个项目只读取接入配置、同步版本和 Beads 任务，单次任务查询限时 8 秒、最多 6 个并行，批次总时限在外层 50 秒内。没有 Beads 或读取失败时不显示零任务。任务更新时间表示记录变化，不代表 Keel/Agent 进程在线。不扫描全盘、不读取密钥、不修改或自动同步其他项目，也不保存第二份项目名单。

项目可选用 `.agents/skills/moi-keel/presentation.json` 提供 Agent 查证后的通俗文案。条目以 `beads:<id>` / `note:<path>` 索引，`revision` 必须与接口返回的来源摘要相同，另有 `title`、`summary` 两个展示字段。摘要不一致时回退来源并提示待更新；这里不保存状态或计划。私有项目的展示内容留在项目内，不写入此适配器。

## 验证

在 `work/` 下运行 `python3 -m unittest tests.test_moi`；完成安装后在项目根运行 `moi bundle`、`moi call-server-fn views/home/getOverview`，并在浏览器检查真实数据和交互。编译成功不代表界面可用。
