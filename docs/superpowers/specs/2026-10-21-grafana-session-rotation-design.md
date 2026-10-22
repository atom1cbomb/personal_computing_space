# Grafana 会话令牌轮换修复

遵循 [spec 设计规范](../spec-design-guidance.md)，复用现有 httpx 客户端、实例锁和私有状态卷。

## 问题与证据

南京生产 Grafana 为 13.2.4。2026-10-21 诊断发现私有 Cookie 仍保存在实例卷中，
但 `/api/datasources` 返回 HTTP 401、`messageId=session.token.rotate`。
Grafana 使用短期令牌并定期轮换，这个响应要求客户端续期，不等同于会话被撤销。
当前网关只接收成功响应附带的 Set-Cookie，未主动执行轮换，将轮换请求误报为
auth_required，导致约十分钟后重新扫码。

Grafana 13.2.4 自身客户端收到该 messageId 后调用
`POST /api/user/auth-tokens/rotate`，再重试原请求。依据：
[官方客户端实现](https://github.com/grafana/grafana/blob/v13.2.4/public/app/core/services/backend_srv.ts)、
[官方会话认证实现](https://github.com/grafana/grafana/blob/v13.2.4/pkg/services/authn/clients/session.go)。

## 行为

网关的健康检查与业务 GET 共用请求流程。只有发送实例会话 Cookie、且收到
401 与明确 `session.token.rotate` 标记时，才向同一配置 origin 的固定轮换路径
POST 一次。接收并保存新的 grafana_session，使用更新后的 Cookie 重试原 GET 一次，
保留原路径与查询参数。整个过程持有已有实例锁，避免健康检查和业务查询同时轮换。

普通 401、会话撤销、登录重定向仍为 auth_required；403、网络或服务错误保持
auth_failed。轮换端点返回真实 401 时需要登录。轮换后重试仍返回轮换挑战等故障
为 auth_failed，不能无限重试，也不能继续宣称 ready。
Bearer 服务账号请求不进入 Cookie 轮换流程。上游返回正文和凭据不进入公开错误。

新 Cookie 使用现有原子写入、0600 权限和实例隔离，重启后继续使用。浏览器回调
验证期间仅在验证成功后持久化。五分钟浏览器挑战、十分钟左右令牌轮换间隔、
实际登录会话生命周期是三个不同概念，前两者均不应周期性要求用户扫码。

## 发布与验证

服务修复以 grafana-http/v4 发布，保留不可变历史包与稳定 Grafana 资源 URI。
升级复制对应实例的私有会话状态到新包实例卷，校验新实例认证和查询后停止旧实例；
客户端使用 Observer 返回的新激活 URI。Observer、SDK 公共接口和业务 action 不变。

回归覆盖浏览器及 Cookie profile 的轮换、原查询参数与重启续用、并发轮换一次、
浏览器回调验证、真实失效、轮换拒绝/网络故障/重复挑战，以及 Bearer 不轮换。
生产验收通过 Observer 查询三个 Grafana 实例，南京跨过至少一次真实令牌轮换周期
仍保持 ready/authenticated，完成 GPU 查询，无需再次扫码。生产会话迁移只在
私有运行环境中完成，不把 Cookie 经由 ContentStore、Run 或聊天传递。
