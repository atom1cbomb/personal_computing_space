# Kubernetes HTTP 能力包设计

**日期：** 2026-09-19
**状态：** 实现设计

## 目标

让安装了一个普通 capability package 的 Slave 能够在 Kubernetes 上执行受限任务，
而不修改 Slave、Observer 或 Driver 的 Kubernetes 专用源码。能力包采用现有的
`service + container:http/1` runtime；Kubernetes 细节封装在一个独立 HTTP gateway
容器内。

## 设计决策

1. package body 只含不可变镜像引用、端口、健康路径和可选的
   `credential_profile` 名称。真实 kubeconfig、OIDC token、宿主机路径不进入
   manifest、content store、digest、事件或日志。
2. Slave operator 在本机配置 credential profile。`container-http-v1` 对 profile
   做严格名称校验，使用固定的只读挂载点 `/run/loom/credentials`；不开放任意
   Docker volume、environment、command 或 device 参数。
3. gateway 固定读取 `/run/loom/credentials/kubeconfig` 和
   `/run/loom/credentials/policy.json`，只能操作 profile 指定 namespace。
4. 任务提交只接受 `batch.volcano.sh/v1alpha1 Job`（VolcanoJob），禁止裸 Pod
   抢占 GPU；资源查询只开放有限的 get/list 操作，不提供任意 kubectl shell。
5. kubeconfig 的 OIDC device-code 认证以 JSON challenge 返回。响应包含
   `verification_uri`、`user_code`、过期时间和轮询间隔；不返回 token，也不由
   Observer 生成二维码图片。客户端可将 `qr_payload`（同一 verification URI）
   自行渲染为二维码。

## Package contract

```json
{
  "package_type": "service",
  "execution": {"kind": "container:http", "version": "1"},
  "body": {
    "image_ref": "registry.example/loom/kubernetes-gateway@sha256:<digest>",
    "container_port": 8080,
    "health_path": "/healthz",
    "credential_profile": "k8s-prod",
    "network_profile": "egress",
    "authentication": {
      "start_path": "/v1/auth/start",
      "status_path": "/v1/auth/status"
    }
  }
}
```

Exports are fixed gateway operations: `/v1/auth/start`, `/v1/auth/status`,
`/v1/resources/list`, `/v1/resources/get`, `/v1/jobs/submit`, `/v1/jobs/status`,
`/v1/jobs/logs` and `/v1/jobs/delete`. Each export still has its normal descriptor
and IoContract refs; adding an operation is a new package version, not a Driver branch.

## Gateway policy

The gateway invokes `kubectl` with an argument vector and a fixed kubeconfig path.
The policy file supplies `namespace` and optionally `context`. Every command gets
`--namespace <policy namespace>` and rejects a request namespace that differs from it.
Object names use Kubernetes DNS-label validation. Submit validates the object kind,
apiVersion, namespace and bounded JSON size before piping it to `kubectl apply -f -`.
Status/log/delete are limited to the submitted VolcanoJob name. Response size and
subprocess timeout are bounded.

Authentication is lazy. An operation that needs a token first runs a bounded probe.
If kubelogin emits a device-code challenge, the gateway extracts only the public URL,
user code and expiry hints and returns `status: "auth_required"`. `/v1/auth/status`
repeats the probe and returns `authenticated` once it succeeds. Challenge state is
short-lived in the gateway process and is never persisted to Observer.

## Deployment

On each Slave, create an operator-owned directory outside Git:

```text
/var/lib/loom/runtime-credentials/k8s-prod/
  kubeconfig       # mode 0600, readable by container UID 65534
  policy.json      # mode 0600, readable by container UID 65534, {"namespace":"dev-<fullname>","context":"..."}
```

Set `LOOM_RUNTIME_PLUGIN_CREDENTIAL_DIR` to the parent directory. Build the gateway
image from `capability_services/kubernetes_http`, push it by digest, replace the
placeholder in the package manifest, upload the manifest through the workspace
capability-package API, and activate it on the target Slave. Never upload the
kubeconfig or policy file to Observer.
Because the Docker Compose `loom-internal` network is intentionally non-egress,
also configure an operator-owned bridge network (for example `loom-egress`) and set
`LOOM_RUNTIME_PLUGIN_EGRESS_NETWORK=loom-egress`. The package only selects the
`egress` profile; it cannot select the network name. Without this setting activation
fails with `service_egress_network_unavailable`.

## Verification

Tests use a fake `kubectl` executable and a temporary profile. They cover profile
path/name validation, fixed Docker mount arguments, challenge redaction, namespace
and VolcanoJob policy, bounded output, and all gateway operations without contacting
a real cluster. Production GPU jobs must be verified with a dry-run or a small
non-GPU VolcanoJob before use.
