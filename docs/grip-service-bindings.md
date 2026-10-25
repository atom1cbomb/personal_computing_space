# GRIP 服务 binding 与字节流

资源发现和管理仍由 Observer 提供。INVOKE 按 DESCRIBE offer 的 binding 访问：服务自带入口、Slave 数据入口和 Observer 中转都可以承载相同的业务契约。Slave 数据入口是可选组件；它与 `/worker/v1/*` 内部控制面分别监听。

包导出的 `runtime_binding` 保留 `path`，增加 `interaction`：

| interaction | GRIP mode | 输入 / 输出 |
|---|---|---|
| task（默认） | request-reply | arguments、expected_revision、idempotency_key / Run URI |
| request-reply | request-reply | IoContract 业务输入 / 业务结果 |
| byte-stream | byte-stream | 建流输入 / stream-open 确认，随后二进制数据 |

`run-package` 可以显式持久执行普通服务动作；流动作不能作为任务节点。服务调用不创建 Run，也不把模型输入输出存入 ContentStore。调用者可以为同一动作选择直连或 Observer 中转，结果语义不变。

SDK 0.8 的 `trusted_bindings` 接收 operator 允许的 protocol、endpoint、audience；`binding_preference` 选择其中已被描述发布的 endpoint 或 audience。显式 endpoint 不会被改写，省略时必须唯一匹配受信路由。`invoke()` 处理 JSON 结果；`open_stream()` 返回支持异步上下文、read/write/aclose 的本地通道对象。默认 HTTP 客户端的认证头、Cookie 不会被转发。发送后失败不会换路重试。

`loom-wss-stream.v1` 每条 WSS 连接只承载一次 INVOKE：先发完整 GRIP 文本请求，接收一个 stream-open 或 error，成功后只允许二进制消息。控制消息与每个二进制消息最大 64 KiB，接收队列最多 16 条，写入等待背压。ping 20 秒、pong 超时 40 秒；建流默认 30 秒或更早的 deadline。此版本不支持半关闭；旧流中断后不会自动重连或重放。授权到期会关闭流。

Observer 用 Ed25519 签发最长一小时的访问凭据，限定主体、受众、workspace、目标、动作、激活实例及修订。中转向后端重新签发受限凭据，保留主体，期限不超过原凭据。验证公钥按 key ID 由 operator 安装。实例停止或配置变化会撤销本地相关调用；跨节点失联时以短期凭据的到期时间限制旧授权。

`configure` 管理动作接收 expected_revision、idempotency_key，以及可选的完整 instance_config/dependencies。dependencies 形式为 `{"kubernetes": {"target": "<完整激活 URI>", "actions": ["<完整流动作 URI>"]}}`。省略字段保持原值，credential_profile 不通过 configure 改绑。响应包含 resource、desired_revision、applied_revision、state；首次尚未应用时 applied_revision 为 null。运行时按配置摘要重建旧容器后才报告新修订 ready。

依赖上下文由 Slave 周期更新到每个实例独立的只读目录，包含描述、Schema、路由与短期凭据。应用使用 `DependencyContext`，可用 min_valid_seconds 为新请求预留授权有效期，不需要 Observer owner token，也不读取其他实例的上游凭据。变更依赖会删除旧上下文并关闭相关流。Observer 暂时不可用时，已有直连与缓存凭据在原期限内工作；本地容器恢复检查不依赖 Observer 注册成功。

K8s v7 增加 services-ensure（task）、service-access-status（request-reply）、open-service-stream（byte-stream）。认证 HOME 位于持久卷 `/var/lib/loom/service/kube`。services-ensure 只管理明确配置且由本服务拥有的普通 ClusterIP Service，检查工作负载 UID、selector 和保存的 Service UID；不采用同名陌生资源或将 headless Service 转换成目标。配置更新可条件替换已拥有 Service，冲突时失败。端口转发运行在能力包内部；网络/进程故障退避恢复，RBAC 和身份配置错误停止重试，认证过期等待重新授权。

embedding-http v1 使用 SDK 的 HTTPX/httpcore stream transport 经 K8s GRIP 通道请求实际模型，复用库解析 HTTP。原生适配使用 AutoPaper 当前契约：`/info` 返回 model/dimension/device，`/embed` 返回 embeddings/dimension，`/embed-one` 返回 embedding/dimension，并保留 normalize/instruction。私有动作在 8080，消费接口在 8081；发布端口只由 operator 批准。

部署配置及切换步骤见 [service-bindings-deployment.md](service-bindings-deployment.md)。没有升级服务端的环境仍以其实际 DESCRIBE 为准。
