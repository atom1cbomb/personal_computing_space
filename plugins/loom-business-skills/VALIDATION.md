# 验证记录：2026-10-30

基线 `396ca67`，分支 `feat/business-skills-plugin`。全部新增文件位于
`plugins/loom-business-skills/`，未修改 Loom 核心、公共 SDK、通用 skill、协议、
生产部署或既有冷启动验证器。

## 自动化与打包

```sh
python -m pytest tests -q --disable-warnings --tb=short
python -m compileall -q src scripts
```

结果：**42 passed，325 warnings，1.37s**。
系统 skill validator 验证入口 `skills/loom-business-memory/SKILL.md` 通过。

覆盖业务步骤 documented/executed/verified 的区分、真实调用证据关联、失效刷新、
显式跨环境重绑定、资源歧义、缺失/离线/权限/认证恢复、多包及循环依赖、schema/
action/interaction/权限/revision/包/目标变化、未完成 Run 保留与拒绝验收、异常信息
脱敏、结果文件隔离、缓存删除和并发更新冲突。测试替身仅实现公开 SDK 接口，不导入
任何 Loom 服务端模块。

上下文专项创建 1,000 条额外经验：默认只返回 3 条摘要，上限 5 条；摘要不包含步骤或
契约正文。插件注册目录始终只有一个 SKILL.md。另一个 Python 进程可以检索、显式
读取单条历史经验，历史视图带 `historical_only=true`。

插件 ZIP 包含根 manifest、一个入口 skill、独立 CLI 源码、按需参考文档和未修改的
SDK 0.10.0 wheel，不包含经验缓存、私有配置、业务数据或服务端代码。将 ZIP 解压到
隔离目录，在新建的 Python venv 中安装其 SDK；`find_spec('loom_v2') is None`，
实际运行打包后的代码，并逐个校验包内源码与工作分支一致。另从解压目录构建并安装
独立 Python wheel，安装后的 `loom-business-skills` CLI 在隔离环境中检索两条经验通过。
打包调整后复核 CLI/跨进程/打包测试，结果 3 passed。

最终插件 ZIP SHA-256：`d3462e29dea2d3bf720125006efab3f4e2c26b48ccf69800a6bfc818b554ca8d`。

## 生产公开接口只读验收

在当前已有、明确选择的命名实例上，由外部客户端生成两条独立经验。原始结果只保存
在私有结果文件，以下记录只保留计数。两条经验均分别通过初次学习和新进程复用，
共四次真实业务读取；没有发布/改变生产资源、执行 SSH、修改文档或处理人工决策。

| 经验 | 实际业务结果与验收 |
| --- | --- |
| `paper-venue-catalog` | AutoPaper `venues_catalog` 返回 95 个支持的会议目录条目；核验 schema、总数及标识字段，不将支持目录解释为已收录论文数 |
| `overleaf-project-discovery` | Overleaf `projects.list` 返回总计 39 个可见项目，本次只读取 3 条；核验 schema、业务 status/error_code、项目标识及分页字段 |

复用从同一私有缓存开启新的 Python 进程、建立新的 SDK client 和网络连接。每条
经验重新读取所有绑定及依赖，随后重新执行真实业务调用，并用新结果验收、记录
verified。生产 CLI 的 `find` 和 `prepare` 也已单独执行通过。

| 场景 | 初次发现、学习及 prepare 请求数 / 耗时 | 新进程检索及 prepare 请求数 / 耗时 | 返回准备上下文字符数（初次 / 复用） |
| --- | --- | --- | --- |
| 会议目录 | 26 / 5,122 ms | 4 / 631 ms | 583 / 581 |
| Overleaf 项目 | 26 / 5,039 ms | 4 / 860 ms | 639 / 637 |

请求数由 httpx 请求事件记录，包含公共 GRIP 的 DESCRIBE/INVOKE 及其 schema 解析。
准备阶段之外，两种模式每个场景的执行和验收均另外用了 19 次 HTTP 请求。缓存减少
的是重新发现和理解契约所需的准备工作，没有省略业务调用或动态状态检查。检索摘要
分别为 137、167 字符；这些字符数不是模型 token 统计。

## 结论范围

已验证插件实现、安装包隔离、跨进程经验复用、真实结果和上述故障/上下文边界。
本轮没有启动独立模型会话做冷启动/复用 A/B，因此不宣称已测得 agent token 节省、
跨模型成功率，或已经完成 issue #1 的全部模型行为验收；原有冷启动验证记录不变。

私有验收脚本、连接文件、经验缓存、原始结果和请求计数位于操作员目录
`production/releases/2026-10-30-business-skills-plugin/`，不进入插件或 Git。
