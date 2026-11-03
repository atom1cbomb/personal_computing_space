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

## 时间、比例和结果单位

| 字段 | 单位和示例 |
|---|---|
| `query` 文本长度 | 运行时最多 32768 个 UTF-8 字节；非 ASCII 文本的字符数可能更少 |
| 查询输入 `time`、`start`、`end` | Unix **秒**（可带小数，也可用数字字符串），或带时区的 RFC3339 字符串，例如 `2026-11-03T08:00:00+08:00`；不能传 Unix 毫秒 |
| `metrics_query_range.step` | 采样间隔，整数秒，至少 `1`；`60` 表示每分钟一次；每条序列最多 11000 个样本 |
| Arena 的 `start` / `end` / `step` | 单位同上；默认结束为现在、开始为结束前 7 天，窗口最多 90 天；默认 step 为 `max(30, ceil(窗口秒数 / 1000))` |
| 输出 `time_range` 和 Prometheus sample 时间戳 | `start`、`end`、样本时间戳为 Unix 秒，`step` 为秒；`queried_at`、`query_time`、`sampled_at` 为带时区的 RFC3339 字符串 |
| `dashboard_url` 内的 `from` / `to` | Grafana 浏览器链接使用 Unix **毫秒**，与 action 的 Unix 秒输入区分 |
| `gpu_metric.unit` / `utilization_percent` | 源指标为 `percent`（0–100）或 `ratio`（0–1）；输出统一为百分比，`50` 表示 50% |
| `gpu_index`、`panel_id`、dashboard version | 设备索引、面板 ID 和版本标识；不表示设备数量或物理量 |
| `gpu_hours` / `unrated_gpu_hours` | GPU 分配小时数 / 缺少显存容量而无法归一化的 GPU 分配小时数；一张 GPU 分配一小时为 1 GPU-hour |
| `a100_80gb_hours` | 按安装显存计算的等效分配小时数：`GPU-hours × installed_vram_gib / 80`；字段名中的 `gb` 实际使用 GiB，缺容量时返回 null |
| `reference_vram_gib` / `accounting_step_seconds` | 归一化基准显存 GiB / 记账采样间隔秒；1 GiB = 1073741824 B，记账间隔独立于查询 step |

通用 `metrics_query`、`metrics_query_range` 和 Arena 原始 sample 数值保留查询表达式的
单位，数值也可能以字符串返回。结合 metric 定义、表达式及 `gpu_arena_describe` 的
`panels[].field_config` 确定单位；百分比、字节、GiB、摄氏度和瓦特不能互换，也不能仅凭
dashboard 显示单位就假定原始数值已完成转换。

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
过滤器，不能传原始正则。`gpu_hours` 是原始分配小时数，`a100_80gb_hours` 是 VRAM
加权分配小时数；它们均不等同于 GPU 利用率、FLOPS 或已验证的个人身份使用量。

调用时遵循当前 DESCRIBE 返回的 action/schema；不要根据 dashboard URL 自行拼接 Grafana
API 路径或替换 activation 的 `api_origin`。
