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

列表结果包含 `items`、`total_count` 和 `complete=true`。`gpu_usage` 默认统计
`nvidia.com/gpu`，返回的是调度资源 request，不是 GPU 利用率、物理容量或个人使用量；
成功/失败 Pod 会排除，无法取得完整 Pod 列表时 action 应失败而不是返回零。

将 activation 绑定到 `computing/volcano` resource 后，从 ready activation 的 DESCRIBE
选择精确 action/schema。`dashboard_url` 是公开导航属性，`api_origin` 是实际运行配置，
修改前者不会改变后者。
