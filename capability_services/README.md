# 通过 GRIP 使用能力服务包

五个服务包都使用同一套流程：发布包 → 描述 Slave → 激活命名实例 → 描述实例 → 调用业务 action → 读取 Run。
能力包是不可变资源，Slave 是执行资源，激活实例也是资源；外部调用方只使用返回的 GRIP URI。
包内的 `/v1/...` 是 `container:http/1` runtime 到容器的内部绑定。

| 目录 | 包版本 | 业务导出 | 激活配置和认证 |
|---|---|---:|---|
| [kubernetes_http](kubernetes_http/README.md) | kubernetes-http/v2 | 6 | `k8s-prod` profile；Kubernetes/OIDC |
| [outline_http](outline_http/README.md) | outline-http/v1 | 12 | `outline-prod/outline_key` |
| [suanlicloud_http](suanlicloud_http/README.md) | suanlicloud-http/v2 | 12 | `suanlicloud-prod/{AccessKeyId,SecretKey}` |
| [vm_ssh_http](vm_ssh_http/README.md) | vm-ssh-http/v2 | 1 | `{host,port,user}`；SSH profile 的 `id_ed25519` |
| [suanli_vm_ssh_http](suanli_vm_ssh_http/README.md) | suanli-vm-ssh-http/v2 | 1 | `{host,port,user}`；opkssh 浏览器认证 |

## 发布

先构建并推送相应目录的 Dockerfile，记录仓库返回的真实 `registry/repository@sha256:...`。
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
{"grip://loom.local/workspaces/workspace-default/packages/suanli-vm-ssh-http/v2": "<刚发布的 SSH 包完整 GRIP URI>"}
```

发布 Suanli Cloud 时用 `--dependencies dependencies.json`，发布工具会校验引用的包坐标和真实 digest。
模板中的 descriptor、Schema、IoContract、validator、依赖包引用也全部使用 GRIP。内容名称包含包名、文件用途和文件名，例如 `content/suanlicloud-http/schemas/instances-list-output/sha256/<摘要>`；动作名称包含包名、版本和操作名。完整可执行 action 身份从发布后的 GRIP 描述读取。

模板使用默认 authority/workspace，发布器会改写成当前客户端配置的命名空间，并根据新内容重新生成摘要。依赖映射的键是模板中的完整包 GRIP 坐标，值是当前命名空间中已发布的包 URI。

## 激活与调用

可运行命令见 [GRIP 示例](../examples/grip/README.md)。SDK 使用方式：

```python
activation = await client.invoke(slave_uri, client.names.action("activate-package"), {
    "package": package_uri,
    "instance_name": "service-main",
    "instance_config": {},
    "idempotency_key": "activate-service-main",
})
description = await client.describe(activation["resource"])
```

`activate-package` 自动创建或复用激活实例资源，`activation["resource"]` 就是其 GRIP URI。
例如算力云默认实例是 `grip://loom.local/workspaces/workspace-default/activations/suanlicloud-http/v2/slave-a/default`。
仅手工启动 Docker 容器不会向 Observer 注册实例；应通过 Slave 的激活动作使用服务包。

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
动作执行返回 `{run: <GRIP URI>}`，按 Run 的状态继续查询结果。复用相同幂等 key 时参数必须保持一致。
停止实例调用其 `stop`，输入 `expected_revision` 和一个新的 `idempotency_key`。
SSH 的成功标准是退出码为 0 且没有超时，两种 SSH 包均携带确定性 validator。

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

集成测试逐个发布五个真实包并执行全部 32 个业务导出，经过 GRIP、Driver、Slave、
真实 `container:http` runtime 和本机 HTTP gateway。Docker、kubectl/SSH 可执行文件及外部 provider
使用测试替身；这些结果不代表生产云、真实 Docker 或浏览器登录验收。
内部 runtime 的 Docker 验收工具仍为 `scripts/accept-container-http.py`；真实部署的公开调用
可使用 `examples/grip/invoke.py`，由调用者明确提供目标、action 和输入。
