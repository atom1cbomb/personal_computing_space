# 通过 GRIP 使用能力服务包

服务包使用同一套流程：发布包 → 描述 Slave → check-package → 激活命名实例 → 描述实例 → 按 offer 调用业务 action。
普通 JSON action 支持直接调用和显式任务执行：直接调用返回业务结果，放入完整计划并通过 `submit-plan` 提交则返回 Run；字节流 action 用 SDK `open_stream` 建立通道。执行方式的选择建议见[通用 skill](../skills/personal-computing-space/SKILL.md#decide-how-an-action-executes)。
调用地址由 binding 决定，支持服务直连和 Observer 中转，见 [服务 binding](../docs/grip-service-bindings.md)。
能力包是不可变资源，Slave 是执行资源，激活实例也是资源；外部调用方只使用返回的 GRIP URI。
包内的 `/v1/...` 是 `container:http/1` runtime 到容器的内部绑定。

能力包部署只需要镜像仓库和 Observer 公开接口，不需要目标节点的 SSH 账号。
在开发机或 CI 构建并推送镜像，由 Slave 自动拉取和启动。数据入口、TLS、签名密钥、
路由和 egress network 在[节点接入](../docs/service-bindings-deployment.md)时准备，供所有包复用。

创建带认证的新能力包时，参照 skill 的
[认证接入指南](../skills/personal-computing-space/references/authentication-authoring.md)。
使用系统统一的 `auth-start` 和 `loom-auth`，在网关内实现供应商认证与凭据验证。

| 目录 | 使用说明 | 构建/部署文档 | 包版本 | 业务导出 | 激活配置和认证 |
|---|---|---|---:|---:|---|
| ark_http | [HOW_TO_USE](ark_http/HOW_TO_USE.md) | [README](ark_http/README.md) | ark-http/v3 | 11 | 用户 API Key profile；Agent Plan／普通 API 模式，多模态理解、图片生成、视频任务、向量化及 Agent Plan HTTP TTS |
| suanli_overleaf_http | [HOW_TO_USE](suanli_overleaf_http/HOW_TO_USE.md) | [README](suanli_overleaf_http/README.md) | suanli-overleaf-http/v2 | 5 | 连接 overleaf.cs.ac.cn；Git token profile + 统一网页登录，项目列表、下载、状态、上传和新项目导入 |
| autopaper_http | [HOW_TO_USE](autopaper_http/HOW_TO_USE.md) | [README](autopaper_http/README.md) | autopaper-http/v5 | 6 | 连接 paper.cs.ac.cn；统一浏览器登录，搜索可选自动抓取入库，提供论文详情及会议/年份查询 |
| kubernetes_http | [HOW_TO_USE](kubernetes_http/HOW_TO_USE.md) | [README](kubernetes_http/README.md) | kubernetes-http/v10 | 9 | 实例选择 kubeconfig/policy profile；认证缓存持久化、失败探测自动恢复，管理 ClusterIP Service 与恢复转发连接 |
| embedding_http | [HOW_TO_USE](embedding_http/HOW_TO_USE.md) | [README](embedding_http/README.md) | embedding-http/v3 | 2 | 通过声明的 Kubernetes 依赖访问模型；提供 GRIP 业务动作及 AutoPaper HTTP 接口 |
| volcano_http | [HOW_TO_USE](volcano_http/HOW_TO_USE.md) | [README](volcano_http/README.md) | volcano-http/v3 | 6 | 实例配置 API 入口；MCUE 描述公开面板地址并关联 Kubernetes 资源，默认跨 namespace 只读查询 |
| grafana_http | [HOW_TO_USE](grafana_http/HOW_TO_USE.md) | [README](grafana_http/README.md) | grafana-http/v7 | 9 | 本地浏览器扫码、会话持久化与自动轮换；仪表盘、Prometheus 指标、GPU 利用率、GPU Arena 查询及 namespace GPU 小时统计 |
| outline_http | [HOW_TO_USE](outline_http/HOW_TO_USE.md) | [README](outline_http/README.md) | outline-http/v5 | 12 | 实例选择 profile，其中含 `outline_key` |
| suanlicloud_http | [HOW_TO_USE](suanlicloud_http/HOW_TO_USE.md) | [README](suanlicloud_http/README.md) | suanlicloud-http/v9 | 12 | 实例选择 profile，其中含 `AccessKeyId`、`SecretKey` |
| vm_ssh_http | [HOW_TO_USE](vm_ssh_http/HOW_TO_USE.md) | [README](vm_ssh_http/README.md) | vm-ssh-http/v7 | 3 | `{host,port,user}`；实例选择 SSH profile，其中含 `id_ed25519` |
| suanli_vm_ssh_http | [HOW_TO_USE](suanli_vm_ssh_http/HOW_TO_USE.md) | [README](suanli_vm_ssh_http/README.md) | suanli-vm-ssh-http/v8 | 3 | `{host,port,user}`；OpenPubkey 浏览器认证、持久自动续期 |

表中版本与导出数量对应各包的 `package.example.json`；已部署实例以 GRIP DESCRIBE 为准。

## 发布

先构建并推送相应目录的 Dockerfile，记录仓库返回的真实 `registry/repository@sha256:...`。
两个 SSH 镜像共用命令执行与文件流模块；SSH、K8s、embedding 和 Overleaf 镜像均安装 SDK。文件流的协议、校验和调用示例见 [VM 文件传输](../docs/vm-file-transfers.md)。
这些镜像都从仓库根目录构建：

```sh
docker build -f capability_services/vm_ssh_http/Dockerfile -t loom/vm-ssh-gateway .
docker build -f capability_services/suanli_vm_ssh_http/Dockerfile -t loom/suanli-vm-ssh-gateway .
docker build -f capability_services/kubernetes_http/Dockerfile -t loom/kubernetes-gateway:v9 .
docker build -f capability_services/embedding_http/Dockerfile -t loom/embedding-gateway:v2 .
docker build -f capability_services/suanli_overleaf_http/Dockerfile -t loom/suanli-overleaf-gateway:v1 .
docker build -f capability_services/ark_http/Dockerfile -t loom/ark-gateway:v2 .
```

配置 `LOOM_OBSERVER_URL`、`LOOM_GRIP_AUTHORITY`、`LOOM_WORKSPACE_ID` 和 `LOOM_GRIP_TOKEN`。
安装仓库 SDK 后，从仓库根目录运行：

```sh
python scripts/publish_service_package.py capability_services/outline_http \
  --image "$OUTLINE_IMAGE_REF"
```

脚本上传 `schemas/`、`validators/`、`contracts/`、`descriptors/`，使用 Observer 返回的
不可变引用完成 manifest，最后输出包的 GRIP URI。无需手工计算或填入 descriptor digest。
包发布响应通过 `resource` 返回 Observer 确定的完整 URI，`ServicePackage.publish` 直接返回这个值。
使用底层 SDK 时，取 `published = await client.register_package(manifest)` 的 `published["resource"]`；
构造 ResourceRef 时配合 `published["package_digest"]`，不要根据包名、版本或客户端命名空间拼接 URI。
本地 HTTP 部署显式加 `--allow-http`。镜像、credential profile 和 egress network 由部署者准备。
发布不会激活容器或调用业务动作。已经发布的包版本不可改写；升级镜像或契约时，应在模板中选择新 `package_version`，并同步依赖映射的坐标。

当前 Suanli Cloud 创建 descriptor 固定引用 SSH v6。依赖映射应填写该版本已发布的 URI；文件传输版 v7 不会自动替换已有创建契约：

```json
{"grip://loom.local/workspaces/workspace-default/packages/suanli-vm-ssh-http/v6": "<已发布 SSH v6 的完整 GRIP URI>"}
```

发布 Suanli Cloud 时用 `--dependencies dependencies.json`，发布工具会校验引用的包坐标和真实 digest。
模板中的 descriptor、Schema、IoContract、validator、依赖包引用也全部使用 GRIP。内容名称包含包名、文件用途和文件名，例如 `content/suanlicloud-http/schemas/instances-list-output/sha256/<摘要>`；动作名称包含包名、版本和操作名。完整可执行 action 身份从发布后的 GRIP 描述读取。

模板使用默认 authority/workspace，发布器会改写成当前客户端配置的命名空间，并根据新内容重新生成摘要。依赖映射的键是模板中的完整包 GRIP 坐标，值是当前命名空间中已发布的包 URI。

## 激活与调用

可运行命令见 [GRIP 示例](../examples/grip/README.md)。SDK 使用方式：

```python
instance_config = {}  # 按所选包的配置 schema 填入必填项和依赖 URI。
check = await client.invoke(slave_uri, client.names.action("check-package"), {
    "package": package_uri, "instance_name": "service-main",
    "instance_config": instance_config,
})
if not check["ready"]:
    raise RuntimeError(check["issues"])

activation = await client.invoke(slave_uri, client.names.action("activate-package"), {
    "package": package_uri,
    "instance_name": "service-main",
    "instance_config": instance_config,
    "credential_profile": "operator-configured-profile",
    "idempotency_key": "activate-service-main",
})
description = await client.describe(activation["resource"])
```

`check-package` 是只读配置检查，返回 required_interactions 和逐项 issues。
它同时检查创建动作的 `attributes_mapping` 和已知实例配置中的依赖值。
依赖缺失或类型错误返回 `activation_config_invalid`，非法引用返回
`dependency_reference_invalid:<字段路径>`。修正配置后重新检查。
缺服务签名为 `service_signing_unavailable`，缺数据入口为 `binding_unavailable`，
入口不合法为 `binding_invalid`。普通 JSON action 的入口问题记录在 issues 中，仍可激活并通过任务执行；
字节流入口问题会阻断激活。通过 `ready` 判断是否满足激活条件，并检查所选执行方式的可用性。
需要修复节点配置时交给节点管理员处理。服务端激活前会重新检查，拒绝请求不创建实例或增加 revision。
配置修复后可以重试原幂等 key；stop 不受这些条件限制。

检查通过不代表镜像拉取、提供方认证或远端网络已通过验收。登录后检查实例
`metadata.available` 和所需动作的 `execution.direct`／`execution.task`；`runtime_state` 是节点报告状态，公开 `state`
会在容器 ready 但存在入口问题时显示 degraded，任务执行仍可能可用。

`activate-package` 自动创建或复用激活实例资源，`activation["resource"]` 就是其 GRIP URI。
例如，按当前模板在默认命名空间激活的算力云实例可为 `grip://loom.local/workspaces/workspace-default/activations/suanlicloud-http/v9/slave-a/default`；实际调用使用返回的 URI。
仅手工启动 Docker 容器不会向 Observer 注册实例；应通过 Slave 的激活动作使用服务包。

`credential_profile` 是激活实例的字段，与 `instance_config` 并列；填 Slave operator 已配置的
profile 名称，不能填密钥、kubeconfig 或宿主机路径。包 manifest 不指定 profile。
同一个 Kubernetes 包可以激活 `beijing` / `k8s-bj` 和 `nanjing` / `k8s-nj` 两个实例，
分别挂载各自的 kubeconfig 和 policy。选择不会改变包摘要，也不会自动出现在公开描述中。
新实例省略或传 null 表示不挂载凭据；浏览器认证的算力云 SSH 实例通常省略此字段。
已有实例省略或传 null 保留原选择，重试、停止再启动及 Slave 重启均保留绑定。
同一实例名不能改绑 profile；切换账号使用新实例名。升级已有部署见
[激活凭据升级说明](../docs/activation-credentials-upgrade.md)。

DESCRIBE 的 `resource.metadata` 包含包、宿主、实例名、状态、revision 和全部业务 `actions`。
算力云包含 12 个业务 action；每项提供 `name`、`description`、完整 `uri`、权限和副作用、
`action_descriptor_ref`、`io_contract_ref`、业务输入 `arguments_schema`、业务输出 `result_schema`。
Schema 引用也都是 GRIP 资源，可用 `client.read_json(ResourceRef.model_validate(ref))` 读取正文。
`offers` 给出当前可调用的业务和管理动作及 binding；`pending_auth`、`failed`、`stopped` 时，
业务动作仍在 `metadata.actions` 中，标记 `available=false` 和 `unavailable_reason`，但没有业务 offer。
凭据、实例配置和内部容器地址不出现在描述中。

### 参数和结果的单位

读取 action 的输入、输出 schema 时，同时读取字段的 `description`。容量、时间、比例、
媒体尺寸以及计数/索引均按所选 action 的字段说明填写；网关保留上游单位，不统一把所有
容量换成 GB，也不根据数值大小猜测秒或毫秒。

| 类型 | 单位与示例 |
|---|---|
| 容量/传输长度 | `B` 是字节；`1 MB = 1000000 B`，`1 GB = 1000000000 B`；`1 MiB = 1048576 B`，`1 GiB = 1073741824 B`。字段要求 B 时，4 GiB 应传 `4294967296` |
| 时长/采样间隔 | `s` 是秒，`ms` 是毫秒，`1 s = 1000 ms`；超时、媒体时长和任务有效期是不同字段 |
| 绝对时间 | Unix 时间戳须注明秒或毫秒；RFC3339 字符串须包含时区，例如 `2026-11-03T08:00:00+08:00` |
| 比例 | `percent` 是 0–100，`ratio` 是 0–1；`50%` 对应 `50` 或 `0.5`，取决于字段声明 |
| 计算资源 | VM `Cpu` 是 vCPU 数；Kubernetes CPU 可写 `500m` 表示 0.5 核；GPU 数量按指定资源类型计数 |
| 媒体 | 图片宽高用像素；视频时长用秒、帧数用帧；采样率 Hz 表示每秒采样数，码率 bps 表示每秒比特数 |
| 数量、索引及无量纲值 | token 数不等于字符数；页码与 item offset 分开解释；设备/面板 ID、随机种子、相似度和模型系数注明各自含义 |

具体字段说明见各包的 `HOW_TO_USE.md` 和 schema。Kubernetes manifest 内的资源 quantity、
Prometheus 查询结果和其他原生上游对象保留各字段的上游单位；指标单位需结合查询表达式、
metric 定义及 dashboard 配置判断。

SSH 包的 `instance_config` 必须提供 `host`、`port`、`user`。
若绑定已登记 VM，额外提供 `target: vm_uri`。同一个包的不同实例使用不同的配置和持久状态。
`pending_auth` 时调用实例的 `auth-start`，输入当前 `expected_revision`；按返回指引完成认证，
重新描述实例确认 ready 后调用业务 action。认证接口不再列作 Kubernetes 业务导出。

普通 JSON action 的直接调用方式：

```python
result = await client.invoke(activation["resource"], selected_action_uri, business_input)
```

`selected_action_uri` 取自 `metadata.actions`，直接调用需要对应 offer 和
`execution.direct.available`。输入与结果遵循业务 Schema；SDK 和 Slave 均进行校验。
同一 action 也能绑定到任务计划，通过 Driver 的 `submit-plan` 获得 Run；参见
[单节点模板](../skills/personal-computing-space/references/single-action-plan.md)。
任务路径保存输入、结果和 Attempt，提交重试使用同一计划与幂等键。
流动作使用 `client.open_stream(target, action, input)`。
停止实例调用其 `stop`，输入 `expected_revision` 和一个新的 `idempotency_key`。
SSH 的成功标准是退出码为 0 且没有超时，两种 SSH 包均携带确定性 validator。

## action 的容错与复合编排

Loom 管理显式提交的 Run／Node／Attempt；能力包实现负责 action 内部业务流程的幂等、错误处理、恢复和补偿。通过任务节点执行时，平台会保存该 action 的输入、执行尝试和结果。能力包内部的普通 HTTP 或 SDK 调用保持为业务实现细节，平台不会自动将它们登记为独立节点。

例如 action 内部顺序调用 A、B、C，在 B 完成后中断。能力包可以自行持久保存进度，通过业务幂等键或结果查询确认 A、B 的状态后继续 C；也可以由具备任务提交权限的包实现或外部调用方，使用 SDK 显式提交一个以 A、B、C 为节点的编排 Run。后者复用 Loom 的持久化、节点结果复用和 Driver 接管机制，复合 action 仍可保留一个对外接口。

选择显式提交编排 Run 时：

- 使用稳定的业务幂等键和相同计划重试；通过 `client.submission(key)` 找回同一次提交及其 Run，保存业务操作与 Run 的关联。
- 在计划中声明所需包、权限和预算，使用稳定的节点步骤标识；绑定已有命名实例时使用精确的 activation 引用。
- 由包实现或调用方处理另行提交的 Run 的等待、超时、取消、结果衔接和清理。当前这些 Run 按独立任务管理，没有自动的父子 Run 生命周期联动。
- 由部署者明确配置可提交任务的调用身份、网络可达性和可信 Pool binding。`DependencyContext` 中声明服务动作的受限凭据不自动授予 Driver 的 `submit-plan` 权限；Driver 编排沙箱继续通过 `ctx.emit_node()` 产生节点。

即使 action 在 Run 内执行，进程在外部服务已成功而结果尚未写入 Attempt 账本时中断，重启后仍可能出现 `attempt_outcome_unknown`。恢复前需由业务实现查询和对账，或进入修复流程。Loom 的执行记录不能替代上游的业务幂等。

包的 `HOW_TO_USE.md` 应说明业务幂等键与重试规则、超时后如何查询结果，以及内部步骤自行容错还是交给显式编排 Run。简单查询可继续使用直接 `request-reply`，按业务需要选择任务记录和恢复粒度。平台职责见[项目说明](../README.md#编排恢复与-action-的责任边界)。

## SSH 公共实现

`vm_ssh_http` 和 `suanli_vm_ssh_http` 共用以下模块，构建时一并复制进能力包镜像：

| 模块 | 职责 | 运行位置 |
| --- | --- | --- |
| [ssh_app.py](ssh_app.py) | 提供 HTTP 认证与命令入口，以及 WebSocket 文件流入口 | 能力包容器 |
| [ssh_execution.py](ssh_execution.py) | 校验命令，执行 SSH，处理超时和输出上限 | 能力包容器 |
| [ssh_files.py](ssh_files.py) | 校验传输参数，连接文件流与 SSH 子进程管道 | 能力包容器 |
| [ssh_files_remote.py](ssh_files_remote.py) | 分块读写文件，校验大小和摘要，原子提交上传 | 目标 VM |

各包的 `server.py` 管理目标地址和认证，向公共模块提供 SSH 连接参数。文件传输时，
`ssh_files.py` 将远端脚本源码通过 SSH 交给 `python3 -c` 执行；目标 VM 需有 Python 3，
无需预装该脚本。协议和调用示例见 [VM 文件传输](../docs/vm-file-transfers.md)。

## SSH 执行限制

普通 VM SSH 与算力云 VM SSH 使用同一执行契约：请求体最多 8 MiB；`command` 最多 1,048,576 个字符；
`stdout`、`stderr` 分别保留前 16 MiB 原始字节，超过则返回 `truncated=true`。
输出边读边截断，超过上限后继续排空管道。长脚本通过 SSH 标准输入交给远端 `sh -s`；
需要 Bash 等其他 shell 时，在脚本中显式调用。

`timeout_seconds` 默认 300 秒，可指定 1–3600 秒；越界会报错，不会静默缩短。
命令超时后终止本地 SSH 进程组，返回 `timed_out=true`、`exit_code=null`；远端独立后台任务
仍应由调用方按任务自身的生命周期管理。输入 Schema 与 action 描述都通过 GRIP 可读。

Slave action 默认预算 3660 秒；插件 invoke 按该预算等待并预留 30 秒；Worker 转发默认等待
3720 秒。HTTP runtime 与插件 invoke 响应默认最多 256 MiB，为两路输出经过 JSON 转义留出空间。
控制请求保留短超时。部署覆盖了 `LOOM_CAPABILITY_ACTION_TIMEOUT_SECONDS`、
`LOOM_WORKER_ACTION_TIMEOUT_SECONDS` 或 `LOOM_RUNTIME_PLUGIN_RESPONSE_MAX_BYTES` 时，应同步检查
这些值。已发布的旧版本不会自动变化，需要构建新镜像、发布当前模板版本并激活新实例。

## 维护与验证

能力包业务术语统一为 `action`：manifest 使用 `action_exports`、`action_descriptor_ref`，
描述符使用 `action.v1`，执行引用使用 `action_ref`，网关响应使用 `{action, result}`。
旧 manifest/描述符和旧镜像需重新生成、构建并以新版本发布；不保留旧业务字段别名。

新增或修改数值字段时，在输入、输出及配置 schema 的 `description` 中说明含义、单位、
适用范围、已知默认值和示例；无量纲系数、计数及标识符也须说明。原生嵌套对象在其所在
字段注明上游单位规则。不要为 `io.v1` 添加未支持的 `unit` 等关键字。
生成的 schema 修改 `scripts/generate_service_contracts.py` 中的定义；SSH 业务及
instance-config schema 直接修改 JSON。同步包的 `HOW_TO_USE.md`，然后重新生成全部内容引用。

修改契约后运行：

```sh
python scripts/generate_service_contracts.py
python scripts/generate_service_contracts.py --check
PATH="$PWD/.venv/bin:$PATH" .venv/bin/pytest -q tests/capability_services tests/api/test_grip.py
```

集成测试发布原有七个包的当前模板，经过 GRIP、Driver、Slave、
真实 `container:http` runtime 和本机 HTTP gateway；另有 embedding 的真实 HTTP/WS 通道测试。
AutoPaper 独立集成测试覆盖发布、激活、浏览器认证、六个业务动作和同实例重新认证。
Overleaf 独立测试覆盖发布、激活、匿名 Cookie 的 pending、五个动作、真实 Git 冲突和
HTTP/WS 文件往返；另有真实容器的只读根目录、持久状态与重启验收。
同一普通 JSON action 分别验证直接回复和经 Driver 的任务执行；字节流单独验证。上述 GRIP 集成测试中的 Docker、kubectl/SSH 可执行文件及外部 provider
使用测试替身；这些结果不代表生产云、真实 Docker 或浏览器登录验收。
内部 runtime 的 Docker 验收工具仍为 `scripts/accept-container-http.py`；真实部署的公开调用
可使用 `examples/grip/invoke.py`，由调用者明确提供目标、action 和输入。

当前源码配套 SDK 为 0.14.0，包含统一登录、重新认证和服务 binding 支持，参见 [认证说明](../docs/authentication.md)
及 [服务部署步骤](../docs/service-bindings-deployment.md)。
