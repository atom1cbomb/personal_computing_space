# Metadata 驱动的外部 Python 编排

> 资源接口历史说明（2026-10-19）：本文记录当时设计；其中资源发现、登记、激活、停用、认证启动和 promote 连带激活的接口已由 [GRIP 资源设计](../specs/2026-10-19-grip-resource-invocation-design.md) 取代。当前使用方法见 [GRIP 指南](../../grip-resource-usage.md)。其余执行与持久化设计按各自适用范围保留。

**日期：** 2026-10-10\
**修订：** 2026-10-15\
**状态：** metadata、公共 SDK/skill、activation 配置校验和持久 cleanup continuation 已实现；本地回归及远端合成资源验收通过，真实 VM/认证尚未端到端验收，见[验收记录](../reports/2026-10-15-metadata-external-agent-validation.md)\
**适用范围：** 外部 coding agent、`loom-orchestration`、资源效果 metadata 和 `container:python_orchestrator/1`\
**相关设计：** [能力包资源效果与后续使用关系](2026-10-09-resource-effect-metadata-design.md)、[外部 Agent 编排与 Driver 执行专用化](2026-10-11-external-orchestration-execution-only-design.md)\
**遵循：** [Spec 设计规范](../spec-design-guidance.md)

本文是 2026-10-11 执行专用化设计的 metadata 使用补充；角色边界、提交协议、宿主接口、认证恢复和执行可靠性均以该设计为准。
本文旧稿中的内置 agent、Observer renderer、`OrchestrationPlan v1`、`metadata_renderer.v1` 和 `orchestration_plan_ref` 设计作废，不保留兼容路径。

## 1. 问题与目标

外部 agent 需要仅凭公共 Observer 数据面发现“创建了什么资源、怎样等待就绪、怎样访问以及怎样清理”，而不是根据包名或 README 猜测资源关系。

本设计将 `resource_effects` 作为编排依据，由外部 agent 生成普通 Python 源码，使用现有公共库构造完整 `ClosureContract` 并一次提交。Observer 只校验、冻结和排队；Driver 只执行冻结程序；业务 capability 负责领域操作和认证协议。

典型 VM 流程属于同一个 Run：

```text
创建 VM
→ 提取身份、登记资源与清理意图
→ 有界查询 VM 就绪状态
→ 创建或复用绑定 VM 的访问 activation
→ 等待认证
→ 执行用户操作
→ 按登记意图清理
```

本设计不新增服务端源码生成器、流程 DSL、第二套 Run 状态机或 provider 专用 Driver 分支。直接提交外部 agent 编写的源码是正式路径，不是过渡方案。

## 2. 职责与传递链

| 角色 | 职责 |
| --- | --- |
| 外部 agent | 发现 metadata，选择资源关系，校验输入映射，编写源码，构造 closure，修复提交诊断 |
| `loom-orchestration` | 提供公共类型、`ProgramSource`、`OrchestratorPackage`、`SubmissionBuilder` 和公共 API 客户端，不替 agent 选择流程 |
| Observer | 公开 metadata，校验契约、引用和准入，冻结 closure，保存执行及资源事实 |
| Driver | 领取 execution request，读取冻结源码，在沙箱内机械执行并中继受控调用 |
| Slave / capability | 执行业务操作，维护 activation、认证和本地执行账本 |

```text
Observer 公共发现 → 外部 agent 生成源码和完整 closure
→ Observer 上传内容、注册 package、接收 run-submission
→ 校验、冻结、排队 → Driver 沙箱执行 → Observer 中继 → Slave
```

源码通过 `POST /api/v1/content` 上传到 Observer，取得 `program_content_ref` 后封装为编排 package。Driver 不接受程序上传，不参与生成或修改源码。已发布的编排包可以直接引用。

## 3. Metadata 发现与校验

agent 读取公共 package、descriptor、IoContract、schema、resource 和 activation 信息；使用 Observer 返回的完整不可变 `ResourceRef`，不手写 digest，不把 operation id 当作 package 身份。

对于选定的 resource effect：

1. `identity_path` 必须存在于创建操作输出 schema；运行时还需验证实际结果包含合法身份。
2. readiness 的 operation ref 必须解析到已发布 package export，其输入 schema 接受映射后的完整输入。
3. readiness 结果必须先按资源身份匹配，再判断状态，不能将另一台 VM 的状态用于当前 VM。
4. access package 必须提供声明的 capability kind，配置映射必须满足该包契约。
5. cleanup operation、输入映射及所需 package 必须可解析，并在 closure 的效果授权和预算内。
6. 引用必须属于当前 workspace 且 digest 匹配。

外部校验用于及早反馈；Observer 仍是引用、schema、workspace 和执行准入的权威。metadata 不能扩大 package allowlist、效果授权或预算。

`resource_effects` v1 只表达受限字段映射、单个 readiness operation、固定 AND 条件以及有界轮询。缺少 metadata、无法解析引用或不支持的谓词必须明确报告，不能猜测条件、固定等待后假定就绪或忽略认证。

对于该子集不能表达的领域逻辑，外部 agent 可以选择已发布的业务 readiness operation 或业务自有编排程序，依据其明确契约编排。没有明确契约时返回 `readiness_unsupported`。Loom 不因此增加服务端 renderer、表达式语言或动态代码生成 plugin。

## 4. 外部源码构造

唯一入口继续为：

```python
async def orchestrate(ctx, input_ref):
    ...
```

程序使用 2026-10-11 spec 第 5.2 节定义的 `OrchestrationContext`。本 spec 不另定义一套接口签名：

| 原语 | 本流程中的作用 |
| --- | --- |
| `emit_node` / `result` | 调用创建、查询、使用及其他已授权能力节点 |
| `read_json` | 读取节点结果并提取资源身份和状态 |
| `sleep` | 按 metadata 间隔等待，单次 0–3600 秒，仍受原 execution deadline 限制 |
| `materialize_json` | 将动态输入物化成受控内容引用 |
| `register_resource` | 显式登记业务资源，返回 `ResourceRef` |
| `defer_cleanup` | 显式登记精确 cleanup operation 和输入映射 |
| `ensure_activation` | 创建或复用绑定资源的具名 activation，返回 `ResourceRef` |
| `wait_activation` | 等待对应 activation 就绪或进入可恢复认证等待 |

有副作用原语及动态节点使用源码作者提供的稳定 `step_id`。多 VM、轮询和多工作节点必须使用可区分、可重放的逻辑标识；同一 execution 内不能用同一个标识提交不同参数。轮询次数、间隔、节点数量和绝对 deadline 必须受预算约束，不依赖进程本地计数实现恢复。

`sleep` 是沙箱内无副作用计时，不新增宿主命令或持久状态。恢复时可重新等待间隔但不延长 execution deadline；不得为了等待开放 `asyncio` 或网络模块导入。

agent 可以在源码中编写普通受限 Python 的字段处理、条件和节点组合，不要求机械生成唯一模板。重复通用代码优先复用已有公共 helper；不为此引入新的计划格式或编译框架。

非敏感连接配置（例如从 VM 状态结果取得的 host、port、user）可以按包契约构造。密码、token、私钥、cookie、授权码不能进入源码、closure、节点输入或 ContentStore。

### 4.1 Resource 与 activation

创建 VM 的结果身份用于显式登记 `ManagedResource`。部署能力包产生的是 activation，不自动成为业务 resource。

已有资源可通过 `GET /api/v1/resources/{resource_id}/activations` 反查访问实例。程序的 `ensure_activation` 复用匹配 package、target resource 和 instance name 的实例；等待和节点执行还要核对当前 revision。

用户操作节点必须带明确的 resource/activation 绑定，不能因为同一 Slave 上有另一个 ready 实例而回退到 default activation。

### 4.2 清理

提取资源身份后先登记资源，再显式调用 `defer_cleanup`，之后才进入 readiness 和访问阶段。`register_resource` 本身不隐式注册清理。

清理意图包含精确操作引用和输入映射，由 Observer 校验并持久化。执行与恢复复用 2026-10-11 spec 的 cleanup continuation 和预算规则；Driver 不从 metadata 自行推导清理顺序或额外操作。

实现复用现有 Run 的 `cleaning_up` 非终态和 execution request 队列。登记顺序即执行顺序，每项预留一个 `max_nodes` 名额；`resource_budget.cleanup_timeout_seconds` 默认 60 秒，范围 1–300，首次进入清理时冻结绝对 deadline，重启不延长。宿主清理映射支持 `$resource.identity`、数组及显式常量；其他创建结果路径由外部源码提取为常量后登记。完成后恢复原终态并写入 `outcome.cleanup`，清理失败不得保留成功终态。

访问 descriptor 的 `instance_config_schema_ref` 指向不可变 object schema；同包多 export 的声明必须一致。发布时校验 metadata 映射，创建 activation 时校验实际配置（含未声明字段），复用时保留已验证配置。`authenticated` 等待条件要求 activation ready 且认证状态确认为 authenticated。

认证等待不触发清理。成功、失败、取消或超时按已登记策略执行清理；无法确认外部副作用时先对账，不盲目重放。清理失败须保留资源身份和错误，不声称资源已释放。业务要求先停访问实例再终止 VM 时，顺序必须由外部程序或明确的清理契约表达。

## 5. 单 Run 认证等待

程序可以包含创建 activation、等待认证和认证后的后续操作，无需拆成两个 Run；它不实现登录协议，也不接收凭据。

1. `wait_activation` 遇到 `pending_auth`，Observer 持久化等待记录，将 Run 和执行请求置为 `awaiting_auth`，清除当前 Driver owner；`outcome` 保持为空。
2. 外部 agent 从公开 `waiting` 投影获取精确 package、activation、Slave、实例、revision 和 deadline，调用公共认证 API，将短期登录信息交给用户或本地认证助手。
3. 普通轮询和编排重放不重新发起登录；认证 health 满足等待条件后，Observer 自动将同一 execution 放回恢复队列。
4. Driver 重放同一冻结程序，复用已确认的 create、resource、activation 和节点结果，不重复创建 VM。

`run_id`、closure version 和逻辑 `execution_id` 不变；单纯认证恢复不递增 execution epoch。等待计入原有 deadline，不延长预算。认证状态不是 `awaiting_decision`，也不通过 resubmit 或手动 resume 完成正常登录恢复。

## 6. 提交与修复

外部 agent 通过 `loom-orchestration` 或等价公共 HTTP 调用：

1. 上传输入、Python 源码、编排 descriptor 和 IoContract，取得真实内容引用。
2. 用 `OrchestratorPackage` 构造现有 `function + container:python_orchestrator/1` manifest，明确 `allowed_node_package_refs`、`max_nodes` 和 `max_live_nodes`；由 Observer 注册并返回 package 身份。
3. 用 `SubmissionBuilder` 构造完整 `ClosureContract`，在现有 `program_systems.package_ref` 中引用编排包并绑定输入。
4. 一次调用 `POST /api/v1/run-submissions`；Observer 校验引用、schema、预算和源码 preflight，原子冻结、排队。
5. 提交被拒绝时，agent 修正本地材料后重试；已创建 Run 的 repair 使用完整替换计划、`base_closure_version` 和公共 resubmit。
6. 通过 submission/Run 查询跟踪结果；HTTP 202 只代表排队，不代表执行成功。

不调用公开 open/patch/readiness/commit/start，不使用 DriverMCP 或消息 API。preflight 是提交内部校验，不是恢复旧的多阶段规划接口。

源码沿用 AST、入口、类型及沙箱检查；运行时继续强制引用、权限、预算和绑定约束。静态 preflight 不声称能证明任意 Python 的业务正确性或所有控制流都完成清理，这些还须通过程序测试和执行证据验收。

## 7. 验收

- **Metadata：** 非法结构、缺失身份字段、schema 不兼容、未知或跨 workspace 引用产生明确诊断；公共发现能够返回完整关系。
- **外部构造：** 使用只安装公共库和 skill 的测试客户端，通过 Observer 发现 metadata、上传源码、注册包并一次提交；不导入 `loom_v2`、不读取本地业务源码或使用内部 endpoint。
- **执行闭环：** fake capability 验证 create → register → defer cleanup → readiness → activate → authenticate → use → cleanup；多 VM 结果和绑定不交叉。
- **恢复：** 认证先完成、等待期间重启、revision 改变、取消及 deadline 不导致重复创建、错误唤醒或遗漏清理证据。
- **边界：** Observer 不生成源码，Driver 不选择流程；非法网络/文件访问、越权节点及凭据进入持久化内容均被拒绝。
- **结果：** 尚未就绪、身份不明和清理失败不能被投影成资源已可用或已释放。

测试以本地 fake capability 和无真实凭据 fixture 为主。真实云 VM 和登录另行取得凭据与预算批准，不以 descriptor 可发现或 schema 校验通过代替端到端验收。

## 8. 实施边界

1. 完成 resource effect 结构及跨引用/schema 校验，补齐公共发现测试。
2. 更新外部 agent 示例和 skill，使其使用 metadata 与现有公共库生成源码、一次提交。
3. 核对现有宿主原语、认证恢复及 cleanup 行为，缺口依照 2026-10-11 spec 补齐，不再并行设计协议。
4. 补充单 Run 资源生命周期和故障恢复验收。

旧稿服务端 renderer 从未落地，不为它新增迁移或兼容实现。
