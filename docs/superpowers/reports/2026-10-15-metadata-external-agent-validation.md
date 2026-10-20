# Metadata 外部编排实施与验收

**日期：** 2026-10-15\
**设计：** [Metadata 驱动的外部 Python 编排](../specs/2026-10-10-metadata-driven-python-orchestration-design.md)\
**结论：** 本地回归及远端外部 agent 的合成资源生命周期通过；真实云 VM 与浏览器认证未做端到端验收。

## 交付与部署

- 公共 `loom-orchestration` SDK 版本为 `0.3.0`；发布 skill ZIP 包含独立 SDK wheel，源码 skill 不提交二进制。
- 外部 agent 在 `9.0.16.66:/root/pcs_test/metadata-20261015` 使用 `skills-v3/personal-computing-space/` 和独立 `.venv`。验证 SDK 版本为 `0.3.0`，`find_spec("loom_v2") is None`。
- 测试目录只接收 skill、SDK 和任务说明，测试程序由远端现有 Codex CLI 编写；没有复制项目源码。部署产物另存 `/opt/loom-production/releases/metadata-20261015/`，不提供给测试 agent。
- `production/deployment.toml` 对应 Observer、Driver、Slave 已重新部署；最终检查三个 `/healthz` 正常，公共 `/api/v1/runtime` 的 execution、orchestration 均为 true。
- 最终服务 wheel SHA256：`dd3497a8c888188f5f206407fd18f7512fe4aceeeb5e5f53a56a95338f45cdc7`。
- 测试 skill ZIP SHA256：`d7f5b0b19f9746482f6960d67fa1da7c963de8e9b9fd2445b3c257ca54a8a46c`。

最终部署包含认证提前完成的唤醒修正、仅匹配最新 activation 等待的修正，以及 execution claim 不再扫描 settled 请求的调整。重新部署后，以下三个 Run 的持久结果均通过公共 API 再次核对。

## 远端外部 agent 结果

Observer：`http://9.0.16.66:8080`。验收采用合成 capability 和合成资源，不创建真实 VM、不读取认证秘密。外部 agent 仅使用公共 API。

| 场景 | Run | Execution | 最终结果 |
| --- | --- | --- | --- |
| 创建、登记后取消 | `run-7a78dad7798e` | `execution-6a5e2cc9feb6` | cancelled；cleanup completed |
| 身份匹配、readiness 条件、使用 | `run-e09038ff9fc2` | `execution-a15fe0ed763f` | completed；4 个节点 completed；cleanup completed |
| 显式 OrchestrationFailure | `run-2c0c0184e816` | `execution-8bc5b45f6486` | failed；cleanup completed |

取消证据包含取消前的 `program_resource_registered` 和 `cleanup_registered`；清理结果保留原身份 `i-synth-v3-v3a1`。身份/readiness 场景实际检查目标身份、running 状态和 IP 存在，未删除断言以使测试通过。三个 Run 的 `outcome.cleanup.errors` 均为空。

远端原始报告为上述测试目录内的 `report-v3.md`、`report-v3.json`；完整执行记录为 `agent-events-v3.jsonl`，证据在 `evidence/v3_*`。v3 是本次验收依据；早期轮次的取消窗口及 readiness 证据不足，不作为对应场景的通过依据。

另已发布并由外部 agent 公共发现验证两个不可变包版本：

- `grip://loom.local/workspaces/workspace-default/packages/suanlicloud-http/metadata-20261015`：create 身份、readiness、cleanup、access requirements 及 schema 均可解析。
- `grip://loom.local/workspaces/workspace-default/packages/suanli-vm-ssh-http/metadata-20261015`：访问配置 schema、输入输出 schema 及 capability kind 可解析。

这两个新版本没有被本次测试激活；metadata 发现成功不等于真实 VM 或登录成功。

## 外部报告疑点复核

保留外部 agent 原始报告，不将其观察直接等同于产品缺陷：

1. **D1：清理后 resource.state 仍为 unknown。** 当前以清理节点结果和 `outcome.cleanup` 表达执行事实；任意 cleanup 操作成功不能自动推断资源业务状态为 released。没有为此增加隐式业务状态推导。
2. **D2：submission 顶层无 disposition/reason。** 终态详情位于 Run 的 `outcome`，公共查询已能读取；没有增加重复投影字段。
3. **D3：带 resource_effects 的空 io.v1 被接受。** 核对 `probe-v3-publish_effects_test` 的实际 io contract（内容摘要 `f1e94f8c190257ab678bc9be4fb3d6424c7c1ba371b8b8fce16e40c80a88a54f`），其 input/output schema 引用均非空，与报告描述不符。另通过公共 API 保留该包的 effects descriptor，改用真正空的 `{"schema_version":"io.v1"}` 注册独立测试包，服务返回 `422 {"detail":"io_contract_invalid"}`。空 schema 校验缺失的结论不成立，该负例没有发布成功。

## 本地验证与限制

- 完整回归：**452 passed，30 skipped**；跳过项包含 Docker/integration/e2e 和既有 skip，不计为通过。
- 新增覆盖包括 metadata/schema/跨引用、activation 配置、单 Run 认证恢复、认证早于等待、最新等待匹配、取消和超时清理、重启复用、未确认执行及残留资源证据。
- 服务 wheel 测试验证运行时静态资源、迁移文件和公共 SDK 的包含关系；contract 生成一致性、skill 校验及 `git diff --check` 通过。
- 远端未验收真实 VM 创建 → 浏览器认证 → SSH 使用；`wait_activation` 和认证恢复由本地 fixture 测试覆盖，不能替代真实认证端到端测试。
- 未执行需要用户明确决定的 `awaiting_decision` 接受/放弃或 repair 流程。
