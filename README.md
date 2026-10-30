# Loom v2

**把分散的计算资源和外部服务，组织成 AI 可以发现、调用和协作使用的个人计算工作空间。**

你可以把自然语言目标交给自己的 coding agent，由它通过 GRIP 发现和调用资源，并将完整执行计划提交给发现的 Driver 资源。服务查询可以直接返回结果，字节流可以连接模型等服务；需要持久化执行的任务由 Observer 校验、冻结计划并排队，Driver 领取执行，再保存结果和执行证据。

它不仅让 AI 给出“应该怎么做”，还提供从需求、计划、执行到结果核验的运行基础。当前面向**单用户、单 Workspace**，可以在一台机器上部署，也可以连接多台执行节点。

## 能做什么

- **通过 agent 发起计算任务**：向外部 agent 描述目标、输入、约束和验收标准；它使用 GRIP 或提交完整计划，Web 界面用于查看执行状态和结果。
- **跨机器执行和组合任务**：将工作拆成多个节点，分发到不同 Slave，再汇总输出。仓库提供了多节点统计分析示例。
- **接入外部服务**：把 Kubernetes、云平台、文档服务等封装成能力包，让 agent 通过明确的接口使用它们，而不是把每个服务的逻辑写进系统核心。
- **使用任务创建的新资源**：例如通过云能力包创建 VM，再为 VM 部署 SSH 访问能力包，完成认证后在 VM 上执行命令。
- **沉淀可复用能力**：任务中生成的能力包可以显式发布，供后续任务复用；也可以直接向 Workspace 上传已有能力包，不必先创建 Run。
- **查询与核验执行事实**：查看可用 Slave、能力包、资源、激活状态、任务尝试和失败原因；按内容引用取回实际结果。

仓库内已有的服务能力包如下。它们需要各自的镜像、网络、服务账号或凭据配置，不会随着 Loom 启动自动获得外部服务权限。

| 能力包（使用说明） | 当前源码版本 | 提供的能力 |
| --- | --- | --- |
| [Kubernetes](capability_services/kubernetes_http/HOW_TO_USE.md) | `kubernetes-http/v8` | 查询和操作声明的集群资源，管理 ClusterIP Service，通过字节流访问服务，持久化认证并恢复连接 |
| [Embedding](capability_services/embedding_http/HOW_TO_USE.md) | `embedding-http/v1` | 通过 Kubernetes 服务依赖调用嵌入模型，提供 GRIP 动作和 AutoPaper HTTP 接口 |
| [Volcano](capability_services/volcano_http/HOW_TO_USE.md) | `volcano-http/v2` | 查询队列、PodGroup 和 GPU 分配，关联对应 Kubernetes 资源 |
| [Grafana](capability_services/grafana_http/HOW_TO_USE.md) | `grafana-http/v5` | 查询仪表盘、Prometheus 指标、GPU 利用率及 GPU Arena，按 namespace 统计 GPU 小时 |
| [AutoPaper](capability_services/autopaper_http/HOW_TO_USE.md) | `autopaper-http/v2` | 统一浏览器登录后搜索论文、读取详情及查询会议和年份 |
| [Suanli Cloud](capability_services/suanlicloud_http/HOW_TO_USE.md) | `suanlicloud-http/v6` | 查询区域、节点、镜像和实例，创建及管理 VM |
| [Suanli VM SSH](capability_services/suanli_vm_ssh_http/HOW_TO_USE.md) | `suanli-vm-ssh-http/v5` | 为单台 VM 提供浏览器认证和 SSH 命令执行动作 |
| [普通 VM SSH](capability_services/vm_ssh_http/HOW_TO_USE.md) | `vm-ssh-http/v5` | 使用挂载的 SSH 密钥访问普通 SSH VM 并执行命令 |
| [Outline](capability_services/outline_http/HOW_TO_USE.md) | `outline-http/v4` | 查询、检索、读取正文、创建和更新文档等受限操作 |

表中版本对应仓库内的包模板；已部署实例的版本和可用动作以 GRIP DESCRIBE 为准。
Grafana 的 GPU 小时按分配时长和显存容量归一化，保留缺失数据与完整性信息；它衡量资源分配，不能直接代表实际计算量或个人使用量。

## 带来什么好处

**已有资源可以协同使用。** 本地服务器、远程 Slave、云 VM 和外部 API 不必使用相同的底层技术；能力包把它们转换成可发现、有输入输出契约的操作。

**业务扩展与系统核心分离。** 新增普通 HTTP 服务能力包时，主要工作是实现服务适配、描述能力和提供契约，不需要修改 Observer、Driver 或 Slave 主体源码。

**AI 的决策与实际执行可以分别检查。** 系统保留执行时使用的计划版本、能力包引用、输入、尝试记录和输出。模型说“完成”不等于任务成功，最终以 Run 状态、结果及验收证据为准。

**一次任务的成果可以复用。** 程序、契约和能力包不必每次重新生成；发布后的包可以被其他 Run 使用，同一个包也可以部署多个有名字的实例。

**错误更容易定位。** 能力未就绪、外部认证失败、执行失败和需要人工验收是不同状态；系统用持久化记录、幂等键和版本检查帮助对账，不把超时直接当作“没有执行”。这不等于对任意外部服务提供 exactly-once 保证。

## 系统如何运行

### 三个角色：Observer、Driver、Slave

```text
用户 / Web UI / 外部 agent
            │ 公共 API、提交与状态查询
            ▼
        Observer ◀──────────▶ Driver
            │                 └─ 执行服务：运行已提交的普通/编排程序
            │ 中继执行命令、接收结果与健康报告
            ├───────────────▶ Slave A
            │                 └─ 执行器 / runtime plugin
            │                      └─ 进程或能力容器 → 外部服务 / VM
            └───────────────▶ Slave B …

状态：Observer → 自己的 PostgreSQL；每个 Slave → 自己的 PostgreSQL
内容：Observer / Driver / Slave → 共享的 MinIO / S3 内容存储
```

**Observer 是公共控制入口和状态权威。** 它保存 Run、计划版本、执行事件、资源和能力包激活记录，接收节点注册与心跳，并中继 Driver 发出的执行请求。它不调用 LLM，也不自行替用户生成业务计划。服务数据入口可由 operator 单独发布，调用方按 Observer 描述的 binding 和受限凭据访问。

**外部 agent 负责编排，Driver 负责执行。** GRIP 目录中的 `computing/driver` 是单一逻辑执行资源，URI 不随 Driver 进程重启变化。外部 agent 对它调用 `submit-plan`，Observer 校验完整 `ClosureContract`、冻结版本并排队；Driver 领取队列项，在沙箱中运行普通程序或 `container:python_orchestrator/1`。它不接收 prompt、不维护会话，也不持有模型 secret。`accepting_submissions` 表示入口可接受提交，`available` 表示 Driver 租约在线；离线时仍可持久化排队。

**Slave 负责实际执行。** 它验证执行请求，在本机通过执行器或 runtime plugin 启动程序、管理能力容器，保存本地执行账本并上报结果。容器由 Slave 侧插件调用 Docker Engine 启动，不是由用户浏览器或 Driver 直接管理。

持久化任务的执行路径是 **Driver → Observer → Slave**。Driver 按计划选择逻辑执行目标，不直接连接 Slave endpoint；Observer 根据已注册的节点信息中继。直接服务调用与字节流按 offer 选择服务入口、Slave 数据入口或 Observer 中转，不经过 Driver。服务内部地址和容器端点由 Slave/runtime 管理，Observer 不需要认识 Kubernetes、Suanli Cloud 等业务协议。

### 从需求到结果

1. **形成任务闭包。** 外部 agent 明确目标和验收标准，准备输入、程序与契约，选择能力包及计算资源。
2. **一次提交计划。** 外部 agent 通过 `loom-orchestration` 的 GRIP `submit-plan` 提交完整闭包；Observer 检查引用、能力绑定、契约和资源就绪条件并冻结版本，返回 Run URI。
3. **排队执行。** Driver 领取不可变执行请求，沙箱产生动态节点并交给 Observer 记录和中继执行。
4. **执行和编排。** 普通任务经 Observer 派发到 Slave；编排沙箱只执行外部 agent 提交的源码，并通过受控原语产生动态节点、资源登记和 activation。
5. **记录结果并验收。** Slave 返回输出、状态和验证证据，结果写入内容存储。Observer 更新 Run；无法自动确认的成功语义会要求人工验收，失败则保留结构化原因。
6. **复用或清理。** 用户可显式发布任务产生的能力包。外部资源的回收应纳入任务流程；关闭 Run 不意味着自动销毁云 VM。

编排程序通过受限的 `read_json`、`materialize_json`、`register_resource`、`ensure_activation`、`wait_activation`、`defer_cleanup`、`emit_node`、`result` 接口读取内容、管理资源并等待结果，不能在编排沙箱里任意访问网络。`container:python_orchestrator/1` 通过同一个 Driver 的 `submit-plan` 接受完整闭包；普通能力包的业务调用仍在 Slave 一侧完成。每个已提交版本有不可变的 `execution/plan` 资源，Run 的 `read` 返回当前计划引用、状态和结果，并可用 `include_history=true` 读取执行证据。

## 统一使用资源：GRIP

Slave、能力包、激活后的实例和 Run 都可通过稳定的 `grip://` URI 发现。使用流程是 `DESCRIBE(target)` 获取动作与 binding，再 `INVOKE(target, action, input)`。例如对 Slave 调用 `run-package`，input 引用能力包与业务输入，结果返回可查询的任务资源。激活实例直接提供自身的业务动作。

包导出的 `runtime_binding.interaction` 决定调用方式：

| 交互类型 | 调用与结果 |
| --- | --- |
| `task` | 提交带业务输入、修订和幂等键的任务，返回 Run URI，再查询状态和结果 |
| `request-reply` | 直接传入业务输入并返回业务结果，不创建 Run |
| `byte-stream` | 使用 SDK `open_stream()` 建立通道，再读写二进制数据 |

任务提交本身也使用 GRIP 的 request-reply mode，调用时应同时读取 offer 的 Schema，不能仅凭 mode 判断结果格式。需要执行记录时，可用 `run-package` 将普通服务动作作为任务运行；字节流动作不能作为任务节点。

SDK 0.8 支持通过 `trusted_bindings` 限定可信入口、通过 `binding_preference` 选择已发布路由。服务授权限定目标、动作、激活修订和有效期；请求发送后失败不会自动换路重放。协议和流限制见 [GRIP 服务 binding 与字节流](docs/grip-service-bindings.md)。

所有资源引用都使用可读 GRIP 名称，包括包内 Schema、IoContract、descriptor 和程序内容。例如 `content/suanlicloud-http/schemas/instances-list-output/sha256/<摘要>`；摘要用于校验，不再单独充当整个资源名字。SDK 和资源目录使用相同 URI。

资源类型使用 `resource.mcue`，例如 `computing/vm`。`metadata.mcue_ref` 指向有版本的 MCUE 定义，
`metadata.attributes` 保存该类型 Schema 校验过的公开属性。登记资源上的 `update-attributes` 支持动态更新名称、说明、标签和硬件描述，并通过 `attributes_revision` 检查并发冲突。SDK 使用 `resources(mcue=...)` 过滤目录。

配置、SDK 示例与实例调用见 [GRIP 资源使用指南](docs/grip-resource-usage.md)。内置服务包的发布、配置与验证见 [服务包指南](capability_services/README.md)，可运行的客户端见 [GRIP 示例](examples/grip/README.md)。SDK 0.10 的计划提交、查询、修复、取消、验收和关闭均使用 GRIP；内容上传、包制作和管理界面保留相应管理接口。

凭据配置由激活实例选择：`activate-package` 的 `credential_profile` 与 `instance_config` 并列，
同包不同实例可以使用不同账号或集群。旧部署引入实例凭据时需执行 Alembic 007，见
[激活凭据升级说明](docs/activation-credentials-upgrade.md)。使用当前服务 binding 时需匹配
SDK 0.8、Observer、Slave 和 `runtime-plugin/2`，见 [节点服务入口部署说明](docs/service-bindings-deployment.md)。

## 能力包、资源和实例

这三个概念分别回答不同的问题：

| 概念 | 回答的问题 | 例子 |
| --- | --- | --- |
| 能力包（Package） | 能做什么，如何调用，如何执行？ | Suanli Cloud API 包、VM SSH 包 |
| 资源（Resource） | 具体使用哪个对象，它由谁管理？ | 某个云服务资源、某一台 VM |
| 激活实例（Activation） | 这个包在什么 Slave 上，以什么配置运行，是否可用？ | `slave-a` 上名为 `vm-example` 的 VM SSH 容器 |

### 能力包提供统一接口

一个包可以导出多个能力。每个导出包含能力描述、输入输出契约（`io_contract`）、效果类别、权限声明和重放安全信息；有实现的包还包含 `body`，例如程序引用或固定 digest 的容器镜像。

输入输出契约不仅描述 JSON 字段，也可以表达成功语义及验证器。**格式正确、HTTP 成功或命令退出成功，不自动等于用户的业务目标已经达成。** 声明了成功语义但没有自动验证器时，系统可以要求人工确认。

SDK 0.9 提供紧凑资源目录、契约校验调用、持久调用预算账本和分布式 map/reduce 构造器，见 [常用工作流](skills/personal-computing-space/references/reliable-workflows.md)。预算账本只约束使用该 helper 的调用，不能替代服务端授权或独立验收。

发布者可以显式选取文件，给服务包附加与不可变镜像关联的只读诊断源码。外部 agent 无需主机 SSH 即可检查参数映射和实现；读取源码不授予修改、激活或替换生产实例的权限。修复应发布新版本并单独验证，源码摘要也不等同于镜像构建证明。流程见 [能力包诊断](skills/personal-computing-space/references/diagnostics.md)。

包还可以只有接口、没有实现：`interface + none/1`、`body=null`。这样的包用于描述资源如何使用，可以查询和发布，但不能安装到 Slave，也不会凭空赋予系统执行能力。需要系统实际调用 VM 时，仍需 VM SSH 这类带实现的包。

能力包可以通过顶层 `how_to_use_ref` 关联一份不可变的 Markdown 使用文档。使用
`ServicePackage.publish` 发布服务包时，包目录下专门的 `HOW_TO_USE.md` 会自动上传并
作为该引用；包目录中的 `README.md` 继续用于构建、部署和开发说明。通过 GRIP 调用
包资源的 `how-to-use` action 可以取得这份包专属说明以及导出的 action/schema 列表。

包的公开描述和运行时实现分离。新增已有 HTTP runtime 支持的业务包，只需交付服务镜像、manifest、descriptor 和 IO contract；如果要增加新的执行方式，则由 operator 安装相应 contract schema 和进程外 runtime plugin，并重启加载，不需要为每种业务修改核心代码。

### 资源类型、描述与访问能力

资源通过 MCUE 定义公开描述属性，通过可选的 `package_ref` 保留来源或接入包，通过可选的 `managed_by_ref` 指向管理者。登记资源不要求先有能力包。使用方通过 GRIP DESCRIBE 一次取得可调用的 offers 与 Schema 引用；Observer 根据实际激活绑定生成执行动作。

例如，一台 VM 的使用包是 VM SSH 包，而管理它的云资源使用 Suanli Cloud 包：**在 VM 上执行命令**和**创建、停止或销毁 VM**是两套不同职责，不需要混在同一个包里。

同一个包可以在同一 Slave 上创建多个命名实例，每个实例绑定自己的目标资源和配置。因此可以为每台 VM 启动一个独立的 SSH 能力容器，分别保存认证状态和 `known_hosts`，不必为每台机器复制一份包版本。

### 发布、检查、激活与认证

包可以在 Run 内作为候选生成，也可以通过 `POST /api/v1/capability-packages` 直接发布到 Workspace。部署流程为：**构建并推送镜像 → 发布包 → 描述 Slave → `check-package` → 激活命名实例 → 认证 → 验证业务动作**。

对目标 Slave 调用 `check-package`，传入包 URI、实例名和拟使用的 `instance_config`，可只读检查节点在线状态、runtime 支持、服务签名、所需路由和依赖填写情况，返回 `ready`、`required_interactions` 和逐项 `issues`。缺服务入口、签名或必填依赖时，服务端会在写入激活前拒绝请求；配置修复后可以重试原幂等键。

资源类型的 MCUE schema 和能力包的配置 schema 可通过 `x-loom-dependency` 标记依赖字段，并用字段说明定义业务含义。DESCRIBE 与 workspace 的 `list-resources` 动作返回实际 `metadata.dependencies`；`how-to-use` 只解释字段与填写要求。创建动作的 `attributes_mapping` 声明如何填入登记属性，发布和激活时做静态检查，登记时验证实际值。详见 [依赖设计](docs/superpowers/specs/2026-10-29-resource-dependency-metadata-design.md) 和 [MCUE 使用说明](skills/personal-computing-space/references/mcue.md)。

检查通过只代表部署配置满足条件，镜像拉取、外部认证和实际网络仍需验证。实例描述中的 `runtime_state` 是节点报告状态；容器已 ready 但必需入口缺失时，公开 `state` 为 `degraded`、`available=false`。调用方应检查实例和所需动作的可用性。

日常能力包部署通过镜像仓库和 Observer 公开接口完成，无需节点 SSH 账号。数据监听、TLS、签名密钥、路由及 egress network 由 operator 在节点接入时统一准备。操作步骤见 [服务包指南](capability_services/README.md)。

需要外部认证的包会报告 `pending_auth`；认证和健康检查通过后才进入 `ready`，后台检查继续更新状态。服务凭据由 operator 通过受控 profile 提供，或由用户完成交互式登录，不应写入包内容、对话或任务输出。

统一认证使用实例的 `auth-start` 与 SDK 的 `loom-auth`，支持设备登录、浏览器回调和浏览器 Cookie 登录；声明同一 IdP 的资源会复用本机受保护的浏览器档案，API key、AK/SK 或 SSH 密钥由 credential profile 提供。浏览器助手运行在用户电脑上：

```text
用户浏览器 ↔ loom-auth ↔ Observer ↔ Slave ↔ 能力容器
                              认证转发不经过 Driver
```

首次登录和凭据失效后的重新认证都从同一客户端发起，重新认证无需创建新激活。助手运行在有浏览器的本机即可；认证绑定远端资源实例，其他机器上的 agent 通过公开接口调用同一已认证实例，无需复制 Cookie 或在测试机重新登录。以实例 DESCRIBE 的当前状态判断是否就绪，助手旧会话过期不等于资源认证失效。浏览器登录需安装 SDK 的 `auth` 可选依赖及 Chromium，详见 [统一认证说明](docs/authentication.md)。助手只转发固定认证消息，短期桥接 token 不进入 Run 或 ContentStore；公网部署应使用 HTTPS 保护这条链路。

## 示例：创建一台 VM 并使用它计算

这些能力可以组合成下面的工作流程：

1. 查询 Workspace 的 Slave、Suanli Cloud 包和可用激活实例。
2. 调用云包创建 VM，保存 InstanceId，并轮询状态和 IP，不能只把创建请求成功当作 VM 已就绪。
3. 在 Observer 登记 VM 资源，关联使用包及管理资源。
4. 在 Slave 上为这台 VM 激活一个有独立名字的 VM SSH 包实例，配置目标地址。
5. 通过 Observer 发起认证，用户运行本地助手并完成浏览器登录；确认实例为 `ready/authenticated`。
6. 通过 VM SSH 包执行计算，取回输出并按任务标准验收。
7. 不再需要时，通过云包终止 VM，停用对应访问实例并更新资源状态。

云 API、SSH 和 OIDC 的专用逻辑都在能力包里；外部 agent 选择并组织这些动作，Driver 执行提交的计划，Observer 负责状态与中继，Slave 负责容器及执行。

当前已分别验证真实云端 VM 创建、浏览器认证和 SSH 执行链路；新本地助手也已验证到 `ready/authenticated`。这些验证**不等于**“coding agent 生成单个 Run 后，从创建、等待人工认证、恢复计算到失败自动清理”的完整无人值守闭环已验收。具体测试边界见 [VM 生产验证记录](docs/superpowers/reports/2026-09-22-vm-browser-ssh-production-validation.md)（其中记录的是早期 SSH 隧道方案）。

## 快速开始

### 本地启动

需要 Docker Engine 和 Docker Compose；本地栈提供共享的 MinIO ContentStore。先准备内部服务密钥和独立的 GRIP token；已有密钥请复用，不要在升级时覆盖：

```bash
mkdir -p secrets
if [ ! -s secrets/internal_api_secret ]; then
  (umask 077; openssl rand -hex 32 > secrets/internal_api_secret)
fi
if [ ! -s secrets/grip_token ]; then
  (umask 077; openssl rand -hex 32 > secrets/grip_token)
fi
export LOOM_GRIP_TOKEN="$(cat secrets/grip_token)"
```

检查 [Compose 配置](deploy/docker-compose.yml) 中 Observer、Driver、Slave 和 ContentStore 配置后启动：

```bash
./scripts/dev-up.sh
```

打开 **http://localhost:18080**。该栈包含 Observer、Driver、两个 Slave、三个独立 PostgreSQL 和 MinIO；Driver 与 Slave 不发布公共端口。外部 agent 的使用流程见 [分布式编排 skill](skills/personal-computing-space/SKILL.md)。

未配置 GRIP token 时，GRIP 接口拒绝访问。本地 SDK 连接 HTTP 入口需显式传入 `allow_insecure_grip=True`；使用当前 shell 中的 token，或让认证客户端读取 token 文件，避免把密钥复制进示例源码。`secrets/`、`.env` 和 `production/` 已被 Git 忽略，实际凭据应保留在这些本地位置或外部密钥存储中。

默认 Compose 没有配置直接服务调用或字节流所需的数据入口与签名。部署包含这些交互类型的包前，按 [节点接入说明](docs/service-bindings-deployment.md) 配置，并通过 `check-package` 核验；仅启动容器不代表服务动作可用。

### 外部 agent 的 SDK 与 skill 分发

`loom-orchestration` 当前版本为 **0.8.1**，是独立 Python 包，源码保留在 `loom_orchestration/`，
不复制到 skill。发布时可将同版本 SDK wheel 附在 skill 中：

```bash
python -m pip install 'setuptools>=68' 'wheel>=0.45'
python scripts/build_agent_skill.py --output dist
```

将生成的 `dist/personal-computing-space.zip` 交给外部 agent，解压后 skill 的
`assets/wheels/` 包含可安装的 SDK；安装方式见其 `references/sdk.md`。

普通 Python 计算的完整发布契约见 [函数包指南](skills/personal-computing-space/references/function-packages.md)。GRIP `mode=request-reply` 表示协议交互方式，业务是否创建 Run 应结合 `interaction` 与输出 Schema 判断。使用已有命名实例时，直接调用该实例，或通过 SDK 0.8.1 的只读 `activation_ref()` 构造编排引用；不要把 Slave 的默认实例调用当作已认证命名实例，详见 [已有实例编排](skills/personal-computing-space/references/existing-activations.md)。
agent 只需 Python 3.12+、该 wheel 及其声明的依赖，无需 Loom 服务端源码。
此命令只构建本地交付物，不上传到包仓库；离线部署需另备依赖 wheels。

metadata 发布校验与发现已支持资源身份、readiness 映射和访问/清理引用检查。
终态 cleanup continuation 复用执行队列，使用预留节点及独立清理 deadline；
`cleaning_up` 结束后须检查 `outcome.cleanup`，未知副作用或失败不会声称资源已释放。
本地 fake capability 验证取消、超时和持久恢复；真实业务生命周期仍须单独验收。

### 常用查询入口

以下入口均由 Observer 承载。GRIP 动作的输入输出格式通过 DESCRIBE 返回的 Schema 资源读取；REST 管理接口可在运行实例的 `/docs` 或 `/openapi.json` 查询。

| API | 用途 |
| --- | --- |
| GRIP Driver `submit-plan` / `get-submission` | 提交完整计划，按业务 `idempotency_key` 查询提交、队列和结果 |
| GRIP Plan `read` | 读取 `plans/{run_id}/{closure_version}` 的不可变闭包 |
| GRIP Run `read` | 查询状态、结果和当前计划；`include_history=true` 返回执行记录 |
| GRIP Run `resubmit` / `cancel` / `resolve` / `close` | 修复计划、取消、验收或放弃、关闭终态任务 |
| `GET /api/v1/runtime` | 查询 Driver 执行运行信息 |
| `POST /.well-known/grip` | 统一描述与调用 Driver、Slave、能力包、激活实例、领域资源、Plan 和 Run |
| `GET /api/v1/capability-packages` | 包制作与完整编排所需的不可变契约、descriptor 和发布记录 |
| `GET /api/v1/content/{digest}` | 取回实际内容，核验 `X-Content-Digest` |

Slave 在线只说明租约有效，不保证空闲容量或指定能力可用。包已发布也不保证某个实例已完成认证，任务仍须通过就绪检查。

SDK 的 `submit(request_id=...)` 参数映射到业务 `idempotency_key`，与 GRIP 消息的 `request_id` 分开。响应丢失后复用同一键和计划，或用 `submission(key)` 查询；返回 Run 只证明已接收，最终以 Run 的状态和证据为准。旧任务 REST 提交、单 Run 查询和生命周期路径已删除，升级需使用 SDK 0.10 与对应 Observer。

### 多机部署与安全边界

复制 [deployment.example.toml](deploy/deployment.example.toml)，填写各角色机器、端口和密钥文件配置，再执行：

```bash
python scripts/deploy_cluster.py --config deploy/deployment.example.toml --dry-run
python scripts/deploy_cluster.py --config deploy/deployment.example.toml
```

远程部署在 `[cluster]` 配置 `grip_authority`、`public_base_url`、`grip_token_file`；token 文件仅作为 Observer secret 挂载，dry-run 会隐藏内容。

服务直连和中转还需配置 `[cluster.service_access]` 的签名、验证公钥及 data/upstream bindings，并在对应 Slave 设置 `grip_data_port`。部署清单会拒绝已声明路由却缺少数据监听或签名密钥的配置；远端 TLS 和连通性仍需实际验证。配置模板与步骤见 [节点服务入口部署说明](docs/service-bindings-deployment.md)。

`[driver]` 仅配置 `workspace_path`。Driver 执行已提交的任务，无需模型服务、Codex/Volcengine 配置或模型 API key；旧的模型配置字段会被拒绝。

目标机需要 Docker/Compose，部署账号需要相应 SSH 和 Docker 权限。这是 **operator 的部署权限**，不是用户使用工作空间或认证外部服务所需的权限。Observer、Driver、Slave 和 MinIO 也可以用 `scripts/deploy_*.py` 分别更新。

当前系统不是多租户公共云平台。公开 API 的访问范围需要部署方通过网络、反向代理及访问控制保护；内部服务使用独立 token。开发 Compose 的数据库和 MinIO 默认密码不能直接用于公网生产环境。

Docker socket 和 runtime plugin 属于受信任的运维边界；容器隔离不应被视作运行任意不可信代码的完整安全保证。访问外部服务需要 operator 配置受控 egress 网络和凭据。数据库、内容存储、Driver 状态及能力容器的持久状态需要分别备份。

## 开发与测试

Python 开发环境要求 3.12 或更新版本：

```bash
python -m venv .venv
.venv/bin/pip install -e '.[test]'
.venv/bin/pytest -q
```

部分集成和 E2E 测试需要运行中的数据库或 Compose 栈。`./scripts/test-e2e.sh` 仅检查已启动栈的基础健康和节点发现，不代表完整任务验收。HTTP 能力包和多节点执行可分别参考 `scripts/accept-container-http.py`、`tests/e2e/`。

外部 agent 的真实使用验收见 [仅凭 skill 的 Codex 验证流程](docs/validation/skill-only-codex.md)：在隔离环境中只提供发布版 skill、任务和连接认证配置，禁止主机 SSH 与项目 checkout；可以通过公开接口读取当前能力包主动发布的诊断源码。按资源发现、远端计算、服务调用及生命周期分别核验。[初轮报告](docs/validation/2026-10-27-skill-only-live.md) 保留原始失败，[修复后完整用例复测](docs/validation/2026-10-27-skill-only-rerun.md) 记录各轮独立结果、认证状态及当前部署限制。

[六任务基线复测](docs/validation/2026-10-27-skill-only-expanded.md) 保留资源漏发现、参数与编排错误、超预算误报通过和凭据输出等问题。[修复与源码诊断复测](docs/validation/2026-10-28-skill-only-repair.md) 记录 Outline 正文修复、Observer 并发状态修复、SDK 0.9、凭据代理及真实任务结果。业务调用成功与完整验收分开统计，目前仍不能宣称仅凭 skill 可以稳定独立交付。

不依赖真实模型服务的测试栈使用 Fake coding agent：

```bash
docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.test.yml \
  --profile test up --build --abort-on-container-exit --exit-code-from driver
```

## 代码与文档导航

- `loom_v2/observer/`：公共 API、权威状态、激活管理和 Slave 中继。
- `loom_v2/driver/`：冻结计划的队列领取、沙箱执行与动态编排运行时。
- `loom_v2/slave/`：执行请求、执行账本和 runtime plugin 宿主。
- `loom_v2/contracts/`：三方共享的任务、资源、能力包及协议契约。
- `runtime_plugins/`、`package_contracts/`：可安装运行时及其 contract schema。
- `capability_services/`：业务能力服务、IO contract 和随包提供的客户端。
- `loom_orchestration/`、`packages/loom-orchestration/`：独立 SDK、GRIP 客户端、认证助手与分发配置。
- `skills/personal-computing-space/`：外部 agent 使用指南及 SDK、认证、服务 binding 参考。
- `loom_v2/content_store.py`、`loom_v2/db/`、`migrations/`：内容存储、数据库模型和 Alembic 迁移。
- `deploy/`、`scripts/`、`tests/`：部署配置、运维脚本与验证。

进一步阅读：[能力包模型](docs/capability-package-model.md)、[Slave 发现 API](docs/observer-slaves-api.md)、[Observer 中继与 VM 编排设计](docs/superpowers/specs/2026-09-22-observer-mediated-execution-and-vm-orchestration-design.md)、[设计规范](docs/superpowers/spec-design-guidance.md)、[竞品对比与 Loom 的特色](docs/competitive-analysis.md)、[Loom 对外讲稿](docs/loom-pitch.md)。历史 spec 记录各阶段决策，具体支持范围以当前实现和验证记录为准。最新的 [能力包部署前检查验证记录](docs/superpowers/reports/2026-10-26-package-deployment-preflight-validation.md) 说明了本地回归范围与生产升级边界。
