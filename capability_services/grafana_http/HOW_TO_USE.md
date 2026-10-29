# Grafana 使用说明

这个包通过已认证的 Grafana activation 查询 dashboard 和 Prometheus 数据源。先调用
`datasources_list`、`dashboards_list` 或 `gpu_arena_describe` 发现实际 UID、panel 和
target，再调用查询 action。所有导出都是 `SafeRead`，不会创建或修改 dashboard、
datasource 或监控数据。

| action | 用途 |
|---|---|
| `datasources_list` | 列出数据源 UID、名称和类型 |
| `dashboards_list` | 分页列出 dashboard |
| `dashboard_get` | 按 dashboard UID 读取面板和变量 |
| `metrics_query` | 执行 instant PromQL |
| `metrics_query_range` | 执行 time-range PromQL |
| `gpu_utilization` | 按 hostname 和可选 GPU index 读取实际利用率 |
| `gpu_arena_describe` | 读取 GPU Arena 面板、target、单位和过滤器 |
| `gpu_arena_query` | 查询指定 Arena panel target |
| `gpu_arena_gpu_hours` | 读取 namespace GPU-hour 排名 |

GPU Arena 查询默认使用最近 7 天，最大时间窗口 90 天；空结果是 `no_data`，不是零。
查询会保留 warnings、samples 和 completeness。`node`、`model`、`gpu_uuid` 是字面值
过滤器，不能传原始正则。GPU-hour 是 VRAM 加权分配时长，不等同于 GPU 利用率、FLOPS
或已验证的个人身份使用量。

调用时遵循当前 DESCRIBE 返回的 action/schema；不要根据 dashboard URL 自行拼接 Grafana
API 路径或替换 activation 的 `api_origin`。
