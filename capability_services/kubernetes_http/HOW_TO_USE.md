# Kubernetes 使用说明

这个包通过 activation 访问 operator 配置的 Kubernetes 集群。激活时选择对应的
`credential_profile`；profile 提供 kubeconfig 和 policy，不能放进 package manifest、
action 参数或资源公开属性。认证模式为 device-code，认证完成后等待 activation
ready，再调用公开 action。

基础 action 包括：

| action | 用途 |
|---|---|
| `resources_list` / `resources_get` | 查询允许范围内的 Kubernetes 资源 |
| `jobs_submit` / `jobs_status` / `jobs_logs` / `jobs_delete` | 管理普通 Kubernetes Job |
| `services-ensure` | 创建或确认本实例拥有的 ClusterIP Service |
| `service-access-status` | 查询受监督的 Service 访问状态 |
| `open-service-stream` | 为已配置且拥有的 Service 打开 byte stream |

## manifest 和结果单位

`jobs_submit.manifest` 与查询返回的资源对象沿用
[Kubernetes quantity 规则](https://kubernetes.io/docs/concepts/configuration/manage-resources-containers/)，
网关原样传递：

| 字段 | 单位和示例 |
|---|---|
| CPU request/limit | CPU 核；`1` 或 `"1000m"` 表示 1 核，`"500m"` 表示 0.5 核 |
| 内存、存储 request/limit/capacity | 字节及 quantity 后缀；`"1Gi"` 为 1073741824 B，`"1G"` 为 1000000000 B；`"512Mi"` 为 536870912 B |
| `nvidia.com/gpu` 等 extended resource | 指定资源的整数数量；MIG 类型按其资源名计数，不与整卡混加 |
| `activeDeadlineSeconds`、`ttlSecondsAfterFinished` | 秒；`300` 表示 5 分钟 |
| `parallelism`、`completions`、`backoffLimit` | 并发数、完成数和重试次数，均为计数 |
| activation `service_targets.*.port` | TCP 端口号，范围 1–65535，例如 `8080`；与目标 Service 端口一致 |
| `service-access-status.checked_at` | 带时区的 RFC3339 时间字符串；尚未检查时为 null |

`vcjobs` 选择 Volcano Job，`jobs` 选择原生 Kubernetes Job。调用前读取每个 action 的
输入 schema；包不提供任意 kubectl、任意 URL 或任意 Docker network 入口。Service
stream 只能访问 activation `configure` 中登记且通过身份校验的 Service，断流不会重放
业务字节。认证失效时对原 activation 调用 `auth-start`，不要新建一个 activation。
