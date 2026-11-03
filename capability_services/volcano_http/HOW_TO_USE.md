# Volcano Dashboard 使用说明

这个包读取已配置 Volcano Dashboard 的 JSON API。activation 的 `instance_config` 只
选择 HTTPS `api_origin`，调用参数不能切换 URL。当前公开 JSON API 不需要登录；如果
activation 报告认证或权限错误，先按当前 DESCRIBE 的认证 action 处理。

| action | 用途 |
|---|---|
| `namespaces_list` | 列出可见 namespace |
| `queues_list` | 查询 queue 状态和搜索结果 |
| `podgroups_list` | 查询 PodGroup |
| `pods_list` | 查询 Pod |
| `jobs_list` | 查询 Volcano Job |
| `gpu_usage` | 统计已调度、运行中和未调度的 GPU request |

## 结果的单位

`allocated`、`running_allocated`、`unscheduled_requested`、`gpu_requests` 都按
`resource_name` 指定的资源计数（默认 `nvidia.com/gpu`）。它们不是 GPU 利用率、显存容量
或统一的物理整卡数；MIG 等资源按各自资源名查询。`pod_count`、`terminal_pods` 和
`by_namespace[].gpu_pods` 是 Pod 个数，列表的 `total_count` 是返回资源条数。
`collected_at` 是带时区的 RFC3339 时间字符串。

原生对象中的 Kubernetes quantity 保留上游单位：CPU `"1000m"` 为 1 核，内存/存储
`"1Gi"` 为 1073741824 B，`"1G"` 为 1000000000 B；其他时间、数量和比例按原字段定义解释。

列表结果包含 `items`、`total_count` 和 `complete=true`。`gpu_usage` 默认统计
`nvidia.com/gpu`，返回的是调度资源 request，不是 GPU 利用率、物理容量或个人使用量；
成功/失败 Pod 会排除，无法取得完整 Pod 列表时 action 应失败而不是返回零。

将 activation 绑定到 `computing/volcano` resource 后，从 ready activation 的 DESCRIBE
选择精确 action/schema。`dashboard_url` 是公开导航属性，`api_origin` 是实际运行配置，
修改前者不会改变后者。
