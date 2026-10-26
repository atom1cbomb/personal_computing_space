# 通过 GRIP 使用能力服务包

服务包使用同一套流程：发布包 → 描述 Slave → check-package → 激活命名实例 → 描述实例 → 按 offer 调用业务 action。
任务 action 返回 Run；请求回复 action 直接返回业务结果；字节流 action 用 SDK `open_stream` 建立通道。
调用地址由 binding 决定，支持服务直连和 Observer 中转，见 [服务 binding](../docs/grip-service-bindings.md)。
能力包是不可变资源，Slave 是执行资源，激活实例也是资源；外部调用方只使用返回的 GRIP URI。
包内的 `/v1/...` 是 `container:http/1` runtime 到容器的内部绑定。

能力包部署只需要镜像仓库和 Observer 公开接口，不需要目标节点的 SSH 账号。
在开发机或 CI 构建并推送镜像，由 Slave 自动拉取和启动。数据入口、TLS、签名密钥、
路由和 egress network 在[节点接入](../docs/service-bindings-deployment.md)时准备，供所有包复用。

创建带认证的新能力包时，参照 skill 的
[认证接入指南](../skills/personal-computing-space/references/authentication-authoring.md)。
使用系统统一的 `auth-start` 和 `loom-auth`，在网关内实现供应商认证与凭据验证。

| 目录 | 包版本 | 业务导出 | 激活配置和认证 |
|---|---|---:|---|
| [autopaper_http](autopaper_http/README.md) | autopaper-http/v1 | 6 | 连接 paper.cs.ac.cn；统一浏览器登录，搜索、论文详情及会议/年份查询 |
| [kubernetes_http](kubernetes_http/README.md) | kubernetes-http/v8 | 9 | 实例选择 kubeconfig/policy profile；认证缓存持久化、失败探测自动恢复，管理 ClusterIP Service 与恢复转发连接 |
| [embedding_http](embedding_http/README.md) | embedding-http/v1 | 2 | 通过声明的 Kubernetes 依赖访问模型；提供 GRIP 业务动作及 AutoPaper HTTP 接口 |
| [volcano_http](volcano_http/README.md) | volcano-http/v2 | 6 | 实例配置 API 入口；MCUE 描述公开面板地址并关联 Kubernetes 资源，默认跨 namespace 只读查询 |
| [grafana_http](grafana_http/README.md) | grafana-http/v4 | 6 | 本地浏览器扫码、实例会话持久化及自动令牌轮换；仪表盘、Prometheus 指标和节点/GPU 编号对应的实际利用率 |
| [outline_http](outline_http/README.md) | outline-http/v3 | 12 | 实例选择 profile，其中含 `outline_key` |
| [suanlicloud_http](suanlicloud_http/README.md) | suanlicloud-http/v5 | 12 | 实例选择 profile，其中含 `AccessKeyId`、`SecretKey` |
| [vm_ssh_http](vm_ssh_http/README.md) | vm-ssh-http/v5 | 1 | `{host,port,user}`；实例选择 SSH profile，其中含 `id_ed25519` |
| [suanli_vm_ssh_http](suanli_vm_ssh_http/README.md) | suanli-vm-ssh-http/v4 | 1 | `{host,port,user}`；opkssh 浏览器认证 |

## 发布

先构建并推送相应目录的 Dockerfile，记录仓库返回的真实 `registry/repository@sha256:...`。
两个 SSH 镜像共用 `capability_services/ssh_execution.py`；K8s 和 embedding 镜像需要 SDK。
这四个镜像都从仓库根目录构建：

```sh
docker build -f capability_services/vm_ssh_http/Dockerfile -t loom/vm-ssh-gateway .
docker build -f capability_services/suanli_vm_ssh_http/Dockerfile -t loom/suanli-vm-ssh-gateway .
docker build -f capability_services/kubernetes_http/Dockerfile -t loom/kubernetes-gateway:v8 .
docker build -f capability_services/embedding_http/Dockerfile -t loom/embedding-gateway:v1 .
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

Suanli Cloud 的创建 VM descriptor 引用 Suanli SSH 使用包。先发布 SSH 包，再创建依赖映射文件：

```json
{"grip://loom.local/workspaces/workspace-default/packages/suanli-vm-ssh-http/v4": "<刚发布的 SSH 包完整 GRIP URI>"}
```

发布 Suanli Cloud 时用 `--dependencies dependencies.json`，发布工具会校验引用的包坐标和真实 digest。
模板中的 descriptor、Schema、IoContract、validator、依赖包引用也全部使用 GRIP。内容名称包含包名、文件用途和文件名，例如 `content/suanlicloud-http/schemas/instances-list-output/sha256/<摘要>`；动作名称包含包名、版本和操作名。完整可执行 action 身份从发布后的 GRIP 描述读取。

模板使用默认 authority/workspace，发布器会改写成当前客户端配置的命名空间，并根据新内容重新生成摘要。依赖映射的键是模板中的完整包 GRIP 坐标，值是当前命名空间中已发布的包 URI。

## 激活与调用

可运行命令见 [GRIP 示例](../examples/grip/README.md)。SDK 使用方式：

```python
check = await client.invoke(slave_uri, client.names.action("check-package"), {
    "package": package_uri, "instance_name": "service-main",
})
if not check["ready"]:
    raise RuntimeError(check["issues"])

activation = await client.invoke(slave_uri, client.names.action("activate-package"), {
    "package": package_uri,
    "instance_name": "service-main",
    "instance_config": {},
    "credential_profile": "operator-configured-profile",
    "idempotency_key": "activate-service-main",
})
description = await client.describe(activation["resource"])
```

`check-package` 是只读配置检查，返回 required_interactions 和逐项 issues。
缺服务签名为 `service_signing_unavailable`，缺数据入口为 `binding_unavailable`，
入口不合法为 `binding_invalid`。检查失败时选择符合条件的节点或交给节点管理员处理，
不要在能力包部署过程中 SSH 补配置。服务端激活前会重新检查，拒绝请求不创建实例或增加 revision。
配置修复后可以重试原幂等 key；stop 不受这些条件限制。

检查通过不代表镜像拉取、提供方认证或远端网络已通过验收。登录后检查实例
`metadata.available` 和所需动作；`runtime_state` 是节点报告状态，公开 `state`
会在容器 ready 但缺必需入口时显示 degraded。已有可用动作仍可调用。

`activate-package` 自动创建或复用激活实例资源，`activation["resource"]` 就是其 GRIP URI。
例如算力云默认实例是 `grip://loom.local/workspaces/workspace-default/activations/suanlicloud-http/v5/slave-a/default`。
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

SSH 包的 `instance_config` 必须提供 `host`、`port`、`user`。
若绑定已登记 VM，额外提供 `target: vm_uri`。同一个包的不同实例使用不同的配置和持久状态。
`pending_auth` 时调用实例的 `auth-start`，输入当前 `expected_revision`；按返回指引完成认证，
重新描述实例确认 ready 后调用业务 action。认证接口不再列作 Kubernetes 业务导出。

以下是任务 action 的调用方式：

```python
run = await client.invoke(activation["resource"], selected_action_uri, {
    "arguments": business_input,
    "expected_revision": description["resource"]["metadata"]["revision"],
    "idempotency_key": "business-call-1",
})
result = await client.invoke(run["run"], client.names.action("read"), {})
```

`selected_action_uri` 必须取自 offers。可以先按 `metadata.actions` 的名称选动作，再匹配其 offer；
对 action 资源 DESCRIBE 也可查看 `metadata.descriptor`。输入以 offer 的 schema 为准。
任务动作返回 `{run: <GRIP URI>}`，按 Run 的状态继续查询结果。复用相同幂等 key 时参数必须保持一致。
直接请求回复动作传业务输入，返回业务结果；流动作使用 `client.open_stream(target, action, input)`。
它们不使用任务的 arguments/revision/idempotency 包装。以 DESCRIBE 的 mode、Schema 为准，
不能把 `request-reply` mode 一律理解成直接业务结果：任务提交本身也是请求回复，结果 Schema 是 Run 引用。
停止实例调用其 `stop`，输入 `expected_revision` 和一个新的 `idempotency_key`。
SSH 的成功标准是退出码为 0 且没有超时，两种 SSH 包均携带确定性 validator。

## SSH 执行限制

普通 SSH v4 与算力云 SSH v3 使用同一执行契约：请求体最多 8 MiB；`command` 最多 1,048,576 个字符；
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

修改契约后运行：

```sh
python scripts/generate_service_contracts.py
python scripts/generate_service_contracts.py --check
PATH="$PWD/.venv/bin:$PATH" .venv/bin/pytest -q tests/capability_services tests/api/test_grip.py
```

集成测试发布原有七个包的当前模板，经过 GRIP、Driver、Slave、
真实 `container:http` runtime 和本机 HTTP gateway；另有 embedding 的真实 HTTP/WS 通道测试。
AutoPaper 独立集成测试覆盖发布、激活、浏览器认证、六个业务动作和同实例重新认证。
任务导出经 Driver 执行，服务导出直接回复或建立流。Docker、kubectl/SSH 可执行文件及外部 provider
使用测试替身；这些结果不代表生产云、真实 Docker 或浏览器登录验收。
内部 runtime 的 Docker 验收工具仍为 `scripts/accept-container-http.py`；真实部署的公开调用
可使用 `examples/grip/invoke.py`，由调用者明确提供目标、action 和输入。

SDK 0.8 包含统一登录、重新认证和服务 binding 支持，参见 [认证说明](../docs/authentication.md)
及 [服务部署步骤](../docs/service-bindings-deployment.md)。
