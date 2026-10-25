# Kubernetes HTTP capability service

Public resource use follows [the shared GRIP workflow](../README.md): publish the
artifact directory with `scripts/publish_service_package.py`, activate the package
on a Slave, then invoke the ready activation's discovered action URI. The HTTP
paths below describe the private runtime-to-container binding.

Build the image and record its immutable digest before creating the package:

```bash
docker build -f capability_services/kubernetes_http/Dockerfile -t registry.example/loom/kubernetes-gateway:1 .
docker push registry.example/loom/kubernetes-gateway:1
docker inspect --format '{{index .RepoDigests 0}}' registry.example/loom/kubernetes-gateway:1
```

Pass the resulting immutable image ref to the shared publisher. Descriptor sources,
schemas and IoContracts are included in this directory; the publisher resolves
and uploads them. Package v8 exports nine business actions. Authentication is
activation lifecycle metadata, invoked publicly through GRIP `auth-start`.
The public `vcjobs` resource selector resolves to `jobs.batch.volcano.sh`;
`jobs` continues to select native Kubernetes Jobs.

The Slave operator must configure the matching profile directory before
activation. The gateway does not read the host user's kubeconfig and does not
expose a generic command endpoint. Since Loom's default internal network has no
egress, configure an operator-owned bridge network and set
`LOOM_RUNTIME_PLUGIN_EGRESS_NETWORK`; the package's `network_profile: egress`
selects that configured network without accepting an arbitrary Docker network.

Select credentials in the activation input, alongside `instance_config`, not
in the package manifest:

```python
activation = await client.invoke(slave_uri, client.names.action("activate-package"), {
    "package": package_uri,
    "instance_name": "beijing",
    "credential_profile": "k8s-bj",
    "idempotency_key": "activate-k8s-beijing",
})
```

Another instance, `nanjing`, can use `credential_profile: k8s-nj` with the same
package URI. Each profile supplies its own `kubeconfig` and `policy.json`; its
context and namespace select the cluster and allowed scope, including Volcano
jobs in that cluster. The runtime mounts the selected directory read-only at
`/run/loom/credentials`. Authentication state is isolated per instance. A retry
or restart preserves the selection; changing profiles requires a new instance
name. The profile name and files are not returned by resource DESCRIBE.

Choose `KUBECTL_VERSION` at image build time to match the target cluster's
supported client version range. The image currently pins kubelogin 1.30.1,
which returns `client.authentication.k8s.io/v1beta1` credentials; the selected
kubeconfig's `users[].user.exec.apiVersion` must use that same value. A `v1`
configuration with this plugin can complete browser login but fail the access
probe because the returned credential API version differs. After correcting a
profile for an activation already marked `failed`, invoke `activate-package`
with the same package, instance and profile and a new idempotency key to retry.
The resource URI is retained and its revision advances; use the returned revision
for subsequent calls.

The action schemas and content-addressed IoContracts live in `schemas/` and
`contracts/`. Regenerate their refs after changing an action boundary with:

```bash
python scripts/generate_service_contracts.py
python scripts/generate_service_contracts.py --check
```

These contracts intentionally leave `success_semantics` null: a 2xx JSON
response that matches the action output schema is the gateway's success
criterion. A future domain-specific criterion must add a deterministic
`success_validator_ref`; adding opaque semantics alone would require
attestation.

## 统一登录与重新认证

`kubernetes-http/v8` 声明认证 `interaction.mode=device-code`，使用
[统一 SDK 登录入口](../../docs/authentication.md)。客户端自动发起认证并展示设备码，
后台仍由 kubeconfig 的 kubelogin 插件授权和刷新凭据。成功结果每 30 秒重新探测。

v8 修复失败探测不再重试的问题，恢复规则为：

- 网络、超时和未知探测失败按 2、4、8、16、32、60 秒退避，后续间隔最多 60 秒。
  退避由健康检查驱动；并发状态查询与服务监督共享同一次探测。恢复后自动变为 ready，
  不要求重新登录、不重新执行业务请求。
- 普通探测默认 30 秒超时（`K8S_GATEWAY_COMMAND_TIMEOUT`）；超时后清理 kubectl
  及其同进程组的凭据插件。只有上次已验证成功的后台复查可以在该期限内保留 ready。
- 明确的凭据拒绝或设备码提示进入 auth_required。有效的设备码进程最多保留 300 秒，
  普通探测的 30 秒超时不会截断扫码等待；健康检查不重启它或延长其期限。过期或退出后
  不再提供旧链接，需要通过同一实例的 auth-start 发起新尝试。
- 明确的权限不足、证书或配置错误保持 auth_failed；修正后使用 auth-start 重新检查。
  不会将这类问题误当成扫码登录能解决的问题。

网关仅记录固定原因码和重试间隔，不记录 provider stderr、token 或 kubeconfig。
私有认证响应的 reason/retryable 用于网关诊断；当前 Observer 的公开认证描述仍只包含
统一协议字段，不会透传这些额外字段。

已发布的 v7 包不可改写；v8 必须以新镜像摘要发布。更新源码不会自动升级生产实例。

## 服务访问与恢复

v7 起将 kubelogin HOME 保存到实例持久卷 `/var/lib/loom/service/kube`，同版本重启复用
登录缓存。各激活实例的缓存独立且按包摘要隔离；升级到 v8 不会自动继承 v7 登录状态。

原有六个任务 action 保留，新增：

| action | 交互 | 用途 |
|---|---|---|
| services-ensure | task，返回 Run | 创建或更新本实例拥有的普通 ClusterIP Service |
| service-access-status | request-reply，直接返回 | 查询连接状态和最近错误 |
| open-service-stream | byte-stream | 向白名单中的 Service 建立一条字节通道 |

使用激活的 `configure` 设置 `service_targets`：键是已登记的 `network/service` GRIP URI，
值声明 namespace、Service 名称/端口、selector 和工作负载 UID。能力包校验工作负载身份，
记录 Service UID，拒绝接管未拥有的 Service，也不会将已有 headless Service 改作转发入口。

转发仅在私有回环地址监听，由监督循环恢复 kubectl 进程；Pod 更换或网络中断使当前请求失败，
后续连接恢复后接受新请求。不会重放业务字节。授权失效进入 `auth_required`，通过原实例
`auth-start` 重新认证。工作负载或 Service 身份变化需要显式更新配置。
认证探测的暂时失败显示为 reconnecting；只有需要用户登录时才显示 pending_auth。

binding、依赖授权与 AutoPaper 完整步骤见 [部署说明](../../docs/service-bindings-deployment.md)。
