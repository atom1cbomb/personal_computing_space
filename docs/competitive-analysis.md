# 竞品对比与 Loom 的特色

**日期：** 2026-09-23
**状态：** 调研记录
**范围：** 对外定位参考。竞品信息取自 2026-09 公开资料，会随时间变化；本文只比较架构定位与能力边界，不评价具体产品的成熟度或质量。

## 1. 竞品地图

Loom 同时落在四个已有赛道里。按层拆开，才能看清哪些是共性、哪些是差异。

| 层 | 代表竞品 | 解决的问题 |
| --- | --- | --- |
| Agent 框架与编排 | LangGraph、CrewAI、AutoGen / Microsoft Agent Framework、Dify、n8n | 在应用进程或单一部署内组织 agent 逻辑与流程 |
| 工具接入与能力发现 | MCP + MCP Registry、A2A Agent Card、Composio、Arcade、Nango、AWS Agent Registry | 让 agent 发现并调用外部工具，管理凭据与授权 |
| 沙箱与执行环境 | E2B、Modal、Daytona、Cloudflare / Vercel Sandbox | 安全运行不可信代码，按会话创建和销毁环境 |
| 分布式执行与工作流引擎 | Temporal、Airflow、Prefect、Dagster、Ray / Anyscale | 持久化、重试、可审计的跨机执行 |
| 算力市场 | Akash、io.net、RunPod、Vast.ai | 采购和出售 GPU 与机器 |
| Agent 运行时（Kubernetes） | kagent、Kubernetes Agent Sandbox、Agent Substrate | 把 agent 作为集群资源声明式部署 |
| 个人 agent 与工作空间 | Manus、Devin、Coze、OpenHands、OpenClaw、Cursor Self-Hosted Machines | 开箱即用的个人或团队 agent |

## 2. 关键判断：「分布式跨机器执行」不是特色

跨机器跑 agent 已经是 agent 框架的标准能力，Loom 在这一维度没有优势：

- AutoGen 官方定位即包含 local and **distributed runtime**，用 actor model 加 gRPC 实现跨机 agent。
- LangGraph / LangSmith Agent Server 提供 task queue 与独立 queue worker，是标准的水平扩展拓扑；Microsoft Agent Framework 1.0 继承了 AutoGen 的分布式 runtime。
- CrewAI AMP 支持 remote crews；n8n queue mode 支持多 worker；Ray 以跨机调度为本职；kagent 与 Agent Sandbox 直接把 agent 当 Kubernetes 资源跨节点运行。

在**规模**维度上 Loom 反而落后：当前没有自动扩缩容，没有 GPU 与推理调度，开发栈只有两个 Slave。把「支持分布式」当作卖点，会直接和 AutoGen、Ray 在对方的主场上竞争。

## 3. 真正的分界：扩展 vs 联邦

Loom 的分布式与框架的分布式回答的不是同一个问题。

| | 框架的分布式 | Loom 的分布式 |
| --- | --- | --- |
| 目的 | **扩展**：负载扛不住时增加机器 | **联邦**：把已存在且互不相同的资源纳入可用执行面 |
| 执行目标 | 同质、可替换、由使用者 provision | 异构、已存在、不归系统所有 |
| 分发单位 | node / task（应用代码中的函数）或 message | `(package_ref, capability_descriptor_ref, input_refs)` 能力引用 |
| 前提 | worker 具备同一份代码与 runtime | Slave 不需要持有 Driver 的代码 |
| 连接方式 | worker 连 queue / broker，或 A2A 直连对方 endpoint | Driver 不连 Slave，由 Observer 中继 |
| 就绪状态 | 启动即就绪 | provision、install、authenticate 三段独立状态 |

一句话概括：**框架的分布式是为了跑得更多，Loom 的分布式是为了用得更多。**

## 4. Loom 的特色

以下能力都由「联邦异构执行面」这一前提推导而来，在框架的 worker 模型里不会自然出现。

### 4.1 组合是契约级，不是代码级

框架中的组合边连接的是同一进程或同一部署内的函数，两端必须共享代码与类型。Loom 的节点选择的是 `(package_ref, capability_descriptor_ref, input_refs)`，两端只通过 `io_contract` 对接。因此两个从未设计过要协作的能力包可以组合，跨机器、跨技术栈、跨组织边界都是同一套机制。

### 4.2 编排是数据，不是有权限的代码

框架的编排图就是执行体，拥有应用进程的完整权限。Loom 的编排运行在受限沙箱中，只暴露 `read_json`、`emit_node`、`result`，不能任意访问网络，产出的是不可变计划版本。组合关系因此可持久化、可复查，执行只认已提交的版本。

### 4.3 异构执行面带来的「就绪」层

当执行目标不归系统所有时，「Slave 在线」不等于「有空闲容量」，也不等于「指定能力已认证」。因此计划提交前需要检查引用、能力绑定、契约和资源就绪条件，能力包也需要 `pending_auth` 到 `ready` 的状态机。框架没有这一层，因为 worker 由使用者自己启动，启动即就绪。

### 4.4 跨机之后，验收才成为必需

执行发生在系统控制不到的地方时，退出码 0 和 HTTP 200 更加不可信。Loom 在契约中表达成功语义与验证器，缺少自动验证器时落到人工验收（`awaiting_decision`），并保留计划版本、能力引用、输入、尝试记录和内容寻址的输出。框架保证「跑到」，观测工具记录「跑过」，Loom 把「达成」做成执行路径上的强制状态。

### 4.5 能力缺口可被物化并复用

组合过程中发现缺少能力时，coding agent 在 Run 内生成候选包，验证后 promote 为 workspace 级可复用包；外部包也可以不经过 Run 直接发布。发布、安装、认证是三个独立步骤，接口包（`interface + none/1`，`body=null`）可以在没有实现的情况下描述资源如何使用。框架的能力面由人预先写死。

### 4.6 运行时零分支扩展

新增普通能力包只需交付镜像、manifest、descriptor 和 IO contract，不需要修改 Observer、Driver 或 Slave 源码。新增执行方式由 operator 安装 contract schema 与进程外 runtime plugin，核心只按 `(package_type, execution.kind, execution.version)` 三元组路由。这套可扩展性建立在能力契约之上，而不是给核心加类型分支。

## 5. 短板

以下是相对竞品的实际差距，对外表述时不应回避：

- **多租户与企业治理**：当前是单用户、单 Workspace，没有 RBAC 与租户隔离；AWS Agent Registry、kagent、TrueFoundry 在这一层更强。
- **生态规模**：MCP Registry 有海量现成 server，Loom 内置能力包只有 Kubernetes、Suanli Cloud、Suanli VM SSH、Outline 四个。
- **隔离强度**：Docker socket 属于受信任的运维边界，容器隔离不等于能运行任意不可信代码；E2B 的 Firecracker 与 Modal 的 gVisor 在隔离上明显更强。
- **算力获取**：Loom 组织已有资源，不采购算力，不覆盖 Akash、RunPod 的领域。
- **可视化编排**：没有图形化编辑器，n8n 与 Dify 在低代码体验上领先。
- **闭环验证**：从创建 VM、等待人工认证、恢复计算到失败自动清理的完整无人值守闭环尚未验收，详见 [VM 生产验证记录](superpowers/reports/2026-09-22-vm-browser-ssh-production-validation.md)。

## 6. 对外表述建议

不把 Loom 描述为「分布式 agent 框架」，而是：

> **面向异构自有资源的联邦式执行底座。** 把互不相干的机器、云 VM 和外部服务通过能力契约变成可组合的执行面，并让组合结果可检查、可验收。

按 agent 框架的尺子衡量，Loom 会显得缺少可视化编排、生态和规模；按「个人计算空间」的尺子衡量，跨机联邦与验收语义是领先的。差异化的战场是**异构、契约、验收**，而不是「分布式」这个词本身。
