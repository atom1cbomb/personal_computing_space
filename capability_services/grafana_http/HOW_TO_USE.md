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
| `gpu_utilization` | 使用显式传入或 activation 已配置的 `gpu_metric`，按 hostname 和可选 GPU index 查询 |
| `gpu_arena_describe` | 读取 GPU Arena 面板、target、单位和过滤器 |
| `gpu_arena_query` | 查询指定 Arena panel target |
| `gpu_arena_gpu_hours` | 读取 namespace GPU-hour 排名 |

## `gpu_utilization` 的前置条件

`hostname` 始终必填。`gpu_metric` 只有在 activation 已配置经过验证的默认映射时才可省略；
否则调用者必须显式传入。显式值覆盖本次调用的默认映射，不修改 activation。
`ready/authenticated` 只表明网关及认证可用，不代表这个动作已配置指标映射。

先通过 `datasources_list`、`dashboard_get` 或 `gpu_arena_describe` 确认数据源、指标及单位，
再用 `metrics_query` 检查真实样本中的节点、GPU index 和 UUID 标签。标签名区分大小写，
`hostname` 与 `Hostname` 不可互换；输入 `hostname` 是节点标签的实际值。

例如，**仅在实际发现以下指标和标签后**，可传入：

```json
{
  "hostname": "node-21",
  "gpu_metric": {
    "name": "DCGM_FI_DEV_GPU_UTIL",
    "node_label": "hostname",
    "index_label": "gpu",
    "uuid_label": "UUID",
    "unit": "percent"
  }
}
```

若运维已在该 activation 的 `instance_config.gpu_metric` 配置同一映射，
本次业务参数可以简写为 `{"hostname":"node-21"}`。`gpu_index` 可选且为非负整数。
`datasource_uid` 可显式指定；省略时使用实例默认值，再按唯一默认或唯一 Prometheus
数据源选择，无法唯一确定时会报错。`unit=ratio` 表示输入指标为 0–1，输出统一为百分比。

| 结果或错误 | 含义 |
|---|---|
| `ok`，利用率为 `0` | 查询成功，实际观测值为零 |
| `no_data` | 映射可用、查询成功，但没有匹配样本；不能解释为零 |
| `gpu_metric_not_configured`，HTTP 400 | 调用未传映射，activation 也没有默认映射；尚未查询 Prometheus |
| `gpu_metric_invalid`，HTTP 400 | 显式映射格式无效 |
| `gpu_metric_sample_invalid`，HTTP 502 | 返回样本不符合映射或数值约束 |

配置、认证、数据源及上游查询错误均不转换为 `no_data`。支持错误透传的运行时会在失败
Run 的 `terminal_error` 中保留服务错误 `code`、`http_status` 和错误类别；不包含原始响应或凭据。

GPU Arena 查询默认使用最近 7 天，最大时间窗口 90 天；空结果是 `no_data`，不是零。
查询会保留 warnings、samples 和 completeness。`node`、`model`、`gpu_uuid` 是字面值
过滤器，不能传原始正则。GPU-hour 是 VRAM 加权分配时长，不等同于 GPU 利用率、FLOPS
或已验证的个人身份使用量。

调用时遵循当前 DESCRIBE 返回的 action/schema；不要根据 dashboard URL 自行拼接 Grafana
API 路径或替换 activation 的 `api_origin`。
