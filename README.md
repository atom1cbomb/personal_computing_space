# Loom v2

Loom v2 是单用户、单 Workspace 的**任务闭包（Task Closure）运行时**。它把「用户想算什么」逐步细化为不可变的执行闭包，交由被显式选中的计算节点执行，再把结果、证据与失败原因回写成可对账的事实。

职责分工只有三条：

- **Observer**：唯一状态权威，也是唯一对外入口。
- **Driver**：唯一语义细化者，把对话变成闭包、把闭包派发出去。
- **Slave**：隔离的执行者，在本地 WorkspaceReplica 上执行被派发的节点。

契约层集中在 `loom_v2/contracts`，是三者共享的唯一真相。

---

## 1. 架构

### 1.1 拓扑

```text
Browser（Conversation-first Web UI）
   │  REST + SSE
   ▼
Observer ── observer-db（PostgreSQL）
   │  唯一公共 API / Run 与事件权威 / 消息收据 / 内容寻址
   │
   ├── /driver/v1/*  ──▶ Driver ── codex app-server（默认后端）
   │                       │         └─ Fake coding-agent（测试后端）
   │                       │ 无独立数据库：线程绑定与请求去重由 Observer 持久化
   │                       └─ Docker socket：编排沙箱
   │
   ├── /worker/v1/*  ──▶ slave-a ── slave-a-db（PostgreSQL）
   └── /worker/v1/*  ──▶ slave-b ── slave-b-db（PostgreSQL）

MinIO / S3（bucket `loom-content`）：Observer、Driver 与各 Slave 共享同一份不可变内容
```

Observer 对外只暴露 `http://localhost:18080`；Driver 与 Slave 停留在私有 Compose 网络内，通过 `x-loom-internal-token` 做服务间鉴权（`loom_v2/auth.py`）。

### 1.2 状态归属

| 角色 | 拥有的状态 | 说明 |
| --- | --- | --- |
| Observer | Run、ClosureVersion、Execution、Attempt、RunEvent、消息收据、能力包、激活记录 | `runs` 表以 JSONB 列保存闭包、事件、包与动态节点 |
| Driver | 无独立数据库 | 线程绑定与请求去重记录由 Observer 代为持久化（`driver_threads`、`driver_requests`） |
| Slave | `slave_attempts`（执行账本）、`slave_replica`（Replica 事实与激活清单） | 不能自行写 Run 终态 |
| 内容层 | 仅 MinIO/S3 | 以 `content://sha256/<digest>` 寻址，写入条件化、读取校验摘要，不覆盖已有对象 |

### 1.3 设计原则

- **单用户单 Workspace**：固定一个 user、一个 workspace、一份 WorkspacePolicy。
- **细化权唯一**：coding-agent 是唯一语义细化者；Observer 与确定性 Runtime 不生成、不修改闭包，也不调用 LLM。
- **执行与细化分离**：`CommittedClosureVersion` 才是执行输入；`start_run` 锁定版本后，后续细化只产生新版本，不静默替换运行中的节点。
- **引用与内容分离**：闭包只接受已绑定的 `ResourceRef`，不接受裸 URL、跨 Slave 本地路径或内联程序体。
- **跨边界幂等**：所有 mutation 使用稳定 operation/idempotency key；超时视为结果未知，只能用同一 key 对账。
- **状态与控制面分离**：每个角色独立 PostgreSQL；Slave 本地库只保存执行账本与 Replica 事实。

---

## 2. 核心概念

### 2.1 Run 与闭包

| 概念 | 含义 |
| --- | --- |
| `ClosureContract` | `open_run` 时固化的高层契约：目标、成功标准、允许效果、资源预算、恢复策略、结果期待 |
| `TaskClosure` | 可细化的任务快照，含 `data`/`program`/`compute` 等应用与系统视图 |
| `ClosureVersion` | 不可变的闭包版本；`draft-*` 为草稿，`committed-*` 为执行输入，`parent_version` 串成细化链 |
| `TypedHole` | 闭包中声明但尚未落实的计算洞，`status` 从 `unbound` 变为 `bound` |
| `ComputeBinding` | 把某个洞绑定到「能力包 + 目标 Slave」的精确实现 |
| `NodeInputBinding` | 把可执行节点绑定到内容寻址的输入 |

细化通过 `loom_apply_plan_patch` 的确定性 patch 完成，支持的操作包括 `set_program_ref`、`set_io_contract_ref`、`set_compute_spec`、`add_typed_hole`、`bind_compute_hole`、`set_execution_payload`、`set_result_expectation`、`add_constraint`、`tighten_constraint`、`materialize_capability_package_candidate`。批量 patch 原子生效，产生一个新的 draft 版本。

### 2.2 能力包

`CapabilityPackageVersion` 是运行时中立的不可变执行信封，而不是裸脚本：

```text
CapabilityPackageVersion {
    package_type / execution{kind, version}     # 唯一选择键三元组
    capability_exports[]                        # descriptor + io contract + effect class
    body                                        # 由对应 package contract 校验
    package_digest                              # sha256
    package_id / package_version
    package_closure_version_ref                 # 包自身闭包标签
    source_run_ref / source_closure_version_ref # provenance
    scope / publication_state / provenance
}
```

内置 contract 三元组：`function + process:json_stdio/1`、`function + container:python_orchestrator/1`、`service + container:http/1`。Operator 可把额外 contract JSON 安装到 `LOOM_PACKAGE_CONTRACT_DIR`。

包的生命周期：

- `run_bound` / `candidate`：Run 内细化产生，只能被来源 Run 引用。
- `workspace_reusable` / `published`：由用户显式 promote 派生出的新版本，可被其他 Run 复用。
- `abandoned`：用户显式放弃，readiness 会以 `capability_package_abandoned` 阻止使用。

候选包不会自动进入 Workspace 能力快照；Runtime 不自动提升或发布能力包。

### 2.3 类型化词汇与 term support

闭包与能力包通过类型化 term 表达能力、精度、预算等要求。内置 registry 注册了 7 种 kind：`loom.compute.capability.v1`、`loom.compute.precision.v1`、`loom.compute.parallelism.v1`、`loom.compute.budget.v1`、`loom.data.locality.v1`、`loom.compute.network.v1`、`loom.compute.deadline.v1`，`refinement_relation` 为 `monotonic`（细化只能收紧）。

Slave 用 `TermSupport` 声明自己「理解到什么程度」，由 `support` × `execution_stages` 决定：

- `support`：`parse`、`preserve`、`match`、`validate`、`enforce`、`discharge`
- `execution_stages`：`commit`、`admission`、`execute`、`terminal`

这是**自报告**，不是已验证事实；Observer 不把它直接当作结论，仍需 health/package evidence 佐证。未注册的 `required` term 会诚实失败，不做 fallback。

### 2.4 分布式执行

动态分布式 Run 使用 `orchestrator_python_v1` 能力包。Driver 在每次 Run 独立的 Docker 沙箱（`--network none`、只读 rootfs、非 root UID、drop capabilities）中执行单文件 `orchestrate(ctx, input_ref)`；受限上下文只暴露 `read_json`、`emit_node`、`result`。每个 `NodeIntent` 都要通过包内 allowlist 校验，再由 Observer 物化为 `DynamicNode`，子节点输出以规范 JSON 存入 ContentStore，Driver 重启后可从持久化事件重放。

提交或启动编排包之前，Observer 会用固定版本 `pyright[nodejs]==1.1.411` 对隔离副本做一次检查。入口必须使用带引号的注解：

```python
async def orchestrate(ctx: "OrchestrationContext", input_ref: "ResourceRef") -> "ResourceRef":
    ...
```

未定义名称与选定类型错误以 `readiness_blocked` 返回完整诊断（rule、message、一基行列号），供 coding-agent 直接修复；readiness 阶段绝不重写或执行源码。

### 2.5 消息与决策

- `POST /api/v1/messages` 是 receipt-first：立即返回 `202`，`accepted` 只代表收据已持久化。
- 收据状态 `accepted`/`queued`/`retryable` 在投影中归一为 `queued`，`in_flight` 归一为 `thinking`。
- 相同 `(workspace_id, request_id)` 配不同内容返回 `409 request_id_reused`；完全相同的重试不会新建 Codex turn。
- 决策只有两类：`attestation`（需用户 `accept` 或 `abandon`）与 `repair`（必须发新消息让 coding-agent 修复，不能 accept）。

---

## 3. 任务生命周期

一次任务从对话消息走到可对账的结果，Run 状态机如下（`loom_v2/observer/repository.py` 的 `RUN_TRANSITIONS`）：

主链：

```text
opened ──refinement_started──▶ thinking ──committed──▶ committed ──execution_started──▶ running
```

从 `running` 出发：

```text
running ──run_succeeded───────▶ completed
running ──run_needs_decision──▶ awaiting_decision
running ──run_failed──────────▶ failed
```

从 `awaiting_decision` 出发（四种去向）：

```text
awaiting_decision ──decision_accepted────▶ completed
awaiting_decision ──decision_abandoned───▶ failed
awaiting_decision ──refinement_started───▶ thinking   （仅当 outcome.decision == "repair"）
awaiting_decision ──run_cancelled────────▶ cancelled
```

收尾：

```text
opened / thinking / committed / running / awaiting_decision ──run_cancelled──▶ cancelled
completed / failed / cancelled ──run_closed──▶ closed
```

完整转移表：

| 当前状态 | 事件 | 目标状态 |
| --- | --- | --- |
| `opened` | `refinement_started` / `run_cancelled` | `thinking` / `cancelled` |
| `thinking` | `committed` / `run_failed` / `run_cancelled` | `committed` / `failed` / `cancelled` |
| `committed` | `execution_started` / `run_cancelled` | `running` / `cancelled` |
| `running` | `run_succeeded` / `run_needs_decision` / `run_failed` / `run_cancelled` | `completed` / `awaiting_decision` / `failed` / `cancelled` |
| `awaiting_decision` | `decision_accepted` / `decision_abandoned` / `refinement_started` / `run_cancelled` | `completed` / `failed` / `thinking` / `cancelled` |
| `completed` / `failed` / `cancelled` | `run_closed` | `closed` |

两处受约束的转移：`awaiting_decision → thinking` 只在 outcome 的 `decision` 为 `repair` 时允许，`decision_accepted` 只在 `decision` 为 `attestation` 时允许（其余情况报 `illegal_state_transition`）。对外投影的会话状态为 `idle`、`thinking`、`executing`、`completed`、`awaiting_decision`、`interrupted`、`failed`。

### 阶段 1：接收与细化

1. 用户在 Conversation 中发送消息，Observer 落收据并异步转交 Driver。
2. coding-agent 澄清目标后调用 `loom_open_run` 固化 `ClosureContract`；Driver 注入并校验 conversation/user/Workspace 身份，Observer 持久化契约。Run 进入 `opened`。
3. agent 通过 `loom_put_content` 上传 JSON Schema、`io.v1` 契约与程序内容，再用 `loom_apply_plan_patch` 逐轮细化闭包。每轮产生一个不可变 draft 版本，Run 进入 `thinking`。
4. 能力缺口由 `materialize_capability_package_candidate` 物化为 `run_bound` 候选包，并通过 `bind_compute_hole` 绑定到具体 Slave。

### 阶段 2：就绪与提交

5. `loom_inspect_plan_readiness` 返回结构化 blockers。检查覆盖洞是否绑定、包是否存在且未被 abandon、包作用域是否对当前 Run 可见、IO contract 是否匹配、目标 Slave 是否在线且支持该 runtime、编排程序的 Pyright 诊断等。
6. 无 blocker 时 `loom_commit_plan` 生成 `committed-*` 版本，Run 进入 `committed`。未提交的草稿永不执行。

### 阶段 3：执行

7. `loom_start_run` 锁定版本，Run 进入 `running`。Driver 经 `WorkerSession` 把信封派发到 `POST /worker/v1/dispatch`，Slave 先 provision 能力包（返回 health report 与激活证据）再执行。
8. 普通执行必须携带已 provision 的能力包（程序内容 + `io.v1` 契约）；未绑包的 `run_code` 请求被拒绝为 `capability_package_required`。默认 Slave 只提供通用 `run_code` executor，没有隐式 fallback。
9. 编排 Run 由沙箱程序发出 `NodeIntent`，Observer 物化为 `DynamicNode`，按节点派发并记录 attempt、execution id 与 epoch。Slave 掉线时在 `allow_reassignment` 允许范围内改派，稳定 TaskRef 与闭包版本不变，新 attempt 与改派原因留痕。

### 阶段 4：结果与决策

10. 成功时 `outcome.disposition = completed`，结果以 `ResourceRef` 返回；应核对 terminal status、输出字段、验收标准、`dynamic_nodes`、验证证据与内容摘要，而不是只看 assistant 文本。
11. 需要人工确认时进入 `awaiting_decision`：`attestation` 需用户显式 `accept`（→ `completed`）或 `abandon`（→ `failed`）；`repair` 必须发新消息修复，不能 accept。
12. 失败或取消时给出结构化原因与证据，不隐藏、不自动重提。
13. 终态 Run 由 `loom_close_run` 关闭（`completed`/`failed`/`cancelled` → `closed`）。

### 阶段 5：能力沉淀

14. Run 终止后用户可 `GET /api/v1/capability-packages` 检查候选包，再显式 promote（派生 `workspace_reusable/published` 新版本，并可选 provision 回传 health report 与激活证据）或 abandon。

---

## 4. 使用生命周期

### 4.1 本地体验

需要 Docker Engine 与 Docker Compose 插件。先准备内部密钥，再启动整套栈：

```bash
mkdir -p secrets
openssl rand -hex 32 > secrets/internal_api_secret
chmod 600 secrets/internal_api_secret
./scripts/dev-up.sh
```

`scripts/dev-up.sh` 会用 `deploy/docker-compose.yml` 启动 MinIO、三个隔离 PostgreSQL（observer-db、slave-a-db、slave-b-db）、Observer、Driver 与两个 Slave。之后打开 `http://localhost:18080` 使用 Conversation-first 界面；Driver 与 Slave 只在内部网络可达。

Driver 默认后端是 codex app-server，模型 `deepseek-v4-flash`，Codex 状态保存在 `driver-codex-state` 卷；默认 base URL 为 `http://host.docker.internal:8787`，可用 `LOOM_CODEX_BASE_URL` 指向其他 OpenAI 兼容端点。Codex API key 可选，仅当上游需要鉴权时使用。宿主模型目录经 `LOOM_CODEX_MODEL_CATALOG_PATH` 挂载，以保留 `deepseek-v4-flash` 等自定义模型元数据；重建 Driver 镜像时设置 `CODEX_VERSION`。

### 4.2 日常使用流程

1. 在 Web UI 新建或选中一个 Conversation；`GET /api/v1/conversations` 列出历史会话，刷新后可恢复消息与 Run 历史。
2. 用自然语言描述目标、输入、期望输出、分布要求、约束与验收标准。不要发送密钥。
3. 发送后轮询 `GET /api/v1/conversations/{conversation_ref}`（或订阅 `/stream` SSE），按 `request_id` 关联本轮消息，以该消息状态为准，而不是只看会话聚合状态。
4. 需要停止时点击 Interrupt 或调用 `POST /api/v1/conversations/{conversation_ref}/interrupt`：已持久化的消息、草稿与事件保留，Run 标记为 `cancelled`，会话投影为 `interrupted`；无活跃 turn 时返回 `409 conversation_not_active`。
5. 需要修正时在同一 Conversation 内用新的 `request_id` 发新消息，不要直接改内部 Run 状态。
6. 出现 `awaiting_decision` 时，先查看最新 Run outcome 与验证证据，再向用户确认 `accept` 或 `abandon`。
7. Run 终止后检查候选能力包，决定 promote 还是 abandon。

外部 coding agent 只应使用 `POST /api/v1/messages`、`/api/v1/conversations/*`、`/api/v1/runs/*` 读取与 interrupt；`/mcp`、`/driver/v1/*`、`/internal/v1/*`、`/worker/v1/*` 是内部面，不属于外部契约。仓库内 `skills/personal-computing-space` 给出了完整的外部 agent 工作流。

### 4.3 确定性测试

```bash
.venv/bin/pytest -q
```

校验 Compose 定义：

```bash
docker compose -f deploy/docker-compose.yml config
docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.test.yml --profile test config
```

测试 profile 使用 Fake coding-agent，不依赖本机 Codex、模型服务或凭据：

```bash
docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.test.yml \
  --profile test up --build --abort-on-container-exit --exit-code-from driver
```

冒烟检查：

```bash
./scripts/test-e2e.sh
```

### 4.4 多机部署

复制 `deploy/deployment.example.toml`，按其中引用创建三个本地密钥文件（`internal_api_secret`、`postgres_password`、`minio_secret_key`）。每个 `[[machines]]` 条目描述一个 SSH 目标与一个角色；控制机会上传仓库、在目标机构建既有 Dockerfile 并在该主机启动 Compose 项目：

```bash
python scripts/deploy_cluster.py --config deploy/deployment.example.toml --dry-run
python scripts/deploy_cluster.py --config deploy/deployment.example.toml
```

目标机需要 Docker Engine、Compose 插件，且配置的 SSH 用户可免密访问 Docker。Observer 与每个 Slave 各自带私有 PostgreSQL 容器与持久卷；MinIO 有独立数据卷；Driver 无数据库，但需要访问 Docker socket 以创建编排沙箱。生成的 URL 使用各机器的 `ssh_host` 与发布端口，因此各机器必须互相可达。

角色可独立升级，无需修改已生成的 endpoint：

```bash
python scripts/deploy_observer.py --config deploy/deployment.example.toml
python scripts/deploy_slave.py    --config deploy/deployment.example.toml --id slave-a
python scripts/deploy_driver.py   --config deploy/deployment.example.toml
python scripts/deploy_minio.py    --config deploy/deployment.example.toml
```

`deploy/deployment.test.toml` 是单机 smoke 拓扑（两个 Slave、Observer、Driver、MinIO），使用 SSH `root@9.0.3.9:22` 与独立发布端口（应用角色 `18080`/`18090`/`18081`/`18082`，MinIO `19000`/`19001`）。

### 4.5 运行期可观测性

- `GET /healthz`、`GET /api/v1/runtime`：就绪状态与当前 Driver / 后端 / 模型。`503 driver_unavailable` 表示暂时无法规划或执行，不要绕过 Observer。
- `GET /api/v1/slaves`：Workspace 内的 Slave 发现视图，`?available_only=true` 只看租约有效节点。`available` 只是最近心跳信号，不代表容量充足或包已激活。
- `GET /api/v1/capabilities`、`GET /api/v1/capability-packages`、`GET /api/v1/runs/{run_id}/capability-packages`：能力与激活视图。
- `GET /api/v1/runs/{run_id}`、`GET /api/v1/runs/{run_id}/readiness`：Run 状态、outcome、动态节点与结构化 blockers。
- `GET /api/v1/content/{digest}`：按摘要取回不可变对象，并核对响应 `X-Content-Digest`。

时间预算由 `LOOM_*` 配置控制：单会话默认 24 小时绝对期限（`LOOM_CODING_AGENT_DEADLINE_SECONDS`），WorkerSession HTTP 调用 90 秒（`LOOM_WORKER_OPERATION_TIMEOUT_SECONDS`），能力执行 30 秒（`LOOM_CAPABILITY_OPERATION_TIMEOUT_SECONDS`）。活跃 turn 期间 Driver 会按 `LOOM_CODING_AGENT_POLL_INTERVAL_SECONDS` 轮询；只有持续 RPC 失败或轮询静默超过 `LOOM_CODING_AGENT_PROTOCOL_FAILURE_SECONDS` 才判定 `coding_agent_stalled`，长时间静默生成是允许的。

---

## 5. 仓库结构

```text
loom_v2/
  contracts/     共享契约：闭包、能力包、term、约束、IO schema、错误信封
  observer/      状态权威：FastAPI 公共 API、repository、派发器、激活、编排预检
  driver/        coding-agent 宿主：MCP 工具面、服务、WorkerSession、编排运行时
  slave/         执行者：Worker API、能力执行器、runtime plugin 宿主
  coding_agents/ CodingAgentProvider 抽象（codex app-server / fake）
  content_store.py  基于 S3/MinIO 的内容寻址存储
  db/            SQLAlchemy 模型与会话；migrations/ 为 Alembic 基线
  web/static/    Conversation-first 前端
deploy/          Compose 定义、单机与多机部署配置
docs/            能力包模型、Slave API 与各期设计文档
scripts/         本地启动、迁移、端到端冒烟与集群部署脚本
skills/          外部 agent 使用 Observer 的工作流说明
```

## 6. 延伸阅读

- `docs/capability-package-model.md`：能力包公共模型、选择键与 runtime 扩展边界。
- `docs/observer-slaves-api.md`：`GET /api/v1/slaves` 的字段语义与限制。
- `docs/superpowers/specs/`：单 Workspace 设计、Run 生命周期、消息可靠性、能力缺口闭环、编排与 HTTP 服务包等设计文档。
- `docs/superpowers/spec-design-guidance.md`：本仓库撰写 spec/设计文档时必须遵循的原则。
