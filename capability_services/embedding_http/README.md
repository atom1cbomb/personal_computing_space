# Embedding HTTP capability service

`embedding-http/v1` 将集群中的 embedding 模型作为可使用的 Loom 服务公开。
两个 GRIP action `embed`、`info` 都直接返回业务结果。模型资源使用 `model/embedding`
MCUE，可通过激活的 `target` 关联；模型名称和维度来自实际 `/info` 结果。

在仓库根目录构建镜像，然后用实际镜像摘要发布包：

```sh
docker build -f capability_services/embedding_http/Dockerfile -t <registry>/embedding-http:v1 .
python scripts/publish_service_package.py capability_services/embedding_http --image '<registry>/embedding-http@sha256:<actual digest>'
```

激活配置包含 `service_ref`（目标 `network/service` URI）、`dependency`（默认 `kubernetes`）
及可选 `request_timeout_seconds`（默认 120 秒）。激活的 `dependencies` 需授权对应 K8s
实例的 `open-service-stream` action。Slave 注入限定范围的短期凭据和描述，不向本容器提供
owner token、kubeconfig 或用户登录状态。详细输入见 [部署说明](../../docs/service-bindings-deployment.md)。

每个模型 HTTP 请求通过 K8s 字节流建立独立连接。HTTP 由 HTTPX/httpcore 处理；
请求不自动重试，断流返回失败，下一次新请求可使用已恢复的 K8s 转发。
输出校验文本条数、向量维度和有限数值，默认最多 256 条文本，响应上限 32 MiB。

AutoPaper 使用独立的容器 8081 消费接口：

| 接口 | 输入或输出 |
|---|---|
| GET /health | `status`、`model_loaded`，实际探测模型 |
| GET /info | `model`、`dimension`、`device` |
| POST /embed | 输入 `texts`、`normalize`、可选 `instruction`；输出 `embeddings`、`dimension` |
| POST /embed-one | 输入 `text`、`normalize`、可选 `instruction`；输出 `embedding`、`dimension` |

只有 operator 的 `consumer_ports` 配置可以将此端口发布到宿主机 127.0.0.1。
私有 runtime API 在 8080，不能作为消费端口发布。实际数据经过依赖 binding 指定的入口，
可使用本机 Slave，也可使用 Observer 中转。2026-10-24 已在生产部署并切换 AutoPaper，
具体入口和验证结果见 [生产记录](../../docs/superpowers/reports/2026-10-24-grip-bindings-production.md)。
