# Embedding 使用说明

这个包把一个已配置的集群 embedding 服务作为 Loom capability 暴露。激活时必须提供
`service_ref`（目标 `network/service` URI）和已授权的 Kubernetes `open-service-stream`
依赖；可选 `request_timeout_seconds` 默认 120 秒。激活绑定目标模型资源后，等待
activation ready，再调用它的 action。

v2 在配置 schema 中声明 `service_ref` 为公开依赖。激活前向 `check-package`
传入拟使用的 `instance_config`；运行时关系可从 activation 的 DESCRIBE 或
workspace `list-resources` 返回的 `metadata.dependencies` 读取。

| action | 输入 | 返回 |
|---|---|---|
| `info` | `{}` | 模型名称、维度和设备信息 |
| `embed` | `texts`、可选 `normalize`、`instruction` | `embeddings` 和 `dimension` |

一次请求最多处理 256 条文本，响应必须通过文本条数、向量维度和有限数值校验。请求
不会自动重试；断流后应重新发起一次新的 `embed` 调用。实际服务地址由 activation
的 dependency binding 选择，业务输入不能覆盖 URL 或 Kubernetes 连接配置。

使用 exact action/schema URI 和当前 activation 的 binding；不要直接访问容器私有端口。
