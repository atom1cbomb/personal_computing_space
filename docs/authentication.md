# 能力包统一认证

统一登录由 SDK 0.7 的 `loom-auth` 提供，适用于 `suanli-vm-ssh-http/v4`、
`kubernetes-http/v6` 和 `grafana-http/v4`。使用匹配的 Observer 和 HTTP runtime。
升级包按新版本发布，保留历史不可变包；重新认证本身不创建新激活。

创建或修改能力包时，参照 skill 中的
[认证接入指南](../skills/personal-computing-space/references/authentication-authoring.md)：
包含系统与能力包职责、manifest、私有 HTTP 接口、实例凭据隔离、过期恢复及验证流程。

## 启动入口

在用户浏览器所在机器安装 SDK 的 `auth` 可选依赖，再安装 Chromium：

```sh
python -m pip install './loom_orchestration-0.7.0-py3-none-any.whl[auth]'
python -m playwright install chromium
python -m loom_orchestration.auth \
  --observer https://observer.example \
  --token-file /private/loom-token \
  --activation grip://loom.local/workspaces/workspace-default/activations/grafana-http/v4/slave-a/bj \
  --activation grip://loom.local/workspaces/workspace-default/activations/kubernetes-http/v6/slave-a/beijing
```

也可使用安装后的 `loom-auth` 命令。Observer 使用 HTTP 时显式加 `--allow-http`。
打开 `http://localhost:3000/`，选择实例，点击登录。可重复 `--activation`，URI
必须来自 Observer。客户端只处理选定的激活实例，凭据文件仅在本地进程读取。

首次登录、登录链接过期、关闭窗口、实际凭据失效，都在同一页面点击“重新认证”。
客户端会重新 DESCRIBE 并调用 `auth-start`，无需重启程序、保存短期会话文件或
再次激活。界面只在同一 revision 的激活达到 ready/authenticated 后显示完成。

## 协议

`auth-start` 输入仍是 `expected_revision`，返回：

```json
{
  "resource": "grip://loom.local/workspaces/workspace-default/activations/grafana-http/v4/slave-a/bj",
  "revision": 1,
  "authentication": {
    "status": "auth_required",
    "method": "browser-cookie",
    "cookie_name": "grafana_session",
    "verification_uri": "http://localhost:3000/",
    "expires_at": 1792583100,
    "poll_after_seconds": 2,
    "client": {
      "bridge_url": "https://observer.example/api/v1/auth/browser/bridge",
      "session_id": "<one-time-id>",
      "session_token": "<private-one-time-token>"
    }
  }
}
```

`verification_uri` 是协议挑战信息；使用统一客户端时直接打开它的主页，客户端
自行管理短期会话。浏览器桥 token 仅用于认证 POST，不进入 Run、ContentStore、
日志或资源描述。DESCRIBE 的 `metadata.authentication` 只返回公开状态和挑战信息，
没有 `client`。五分钟是浏览器挑战期限，不是实际凭据期限。

包声明 `authentication.interaction.mode`：`redirect` 捕获固定本地 OAuth 回调；
`browser-cookie` 从自身隔离浏览器读取指定 cookie，并由服务端验证；`device-code`
由原凭据插件完成设备授权。`credential-profile` 用于 Outline、算力云 API 和普通
SSH 的操作员凭据配置，只验证凭据，不发起浏览器授权。Grafana 保留原站回调，SSH 保留 opkssh 登记的回调。

## 状态和恢复

凭据缺失/失效为 auth_required（激活 pending_auth）；认证或连接故障为 auth_failed
（运行中的激活 degraded）；认证成功为 authenticated，实际服务健康后为 ready。
已有 failed 实例只接受显式 auth-start 的可信结果恢复，普通健康上报不能解除 failed。
停止的实例不能认证，revision 变化会使旧会话失效。

Kubernetes 定期重新执行权限检查，允许 kubelogin 使用自身刷新机制；有效凭据的
后台重检在命令超时内保持上次成功状态，新挑战或失败立即生效。Grafana
在 v4 中处理 `401 + messageId=session.token.rotate`：向固定同源
`POST /api/user/auth-tokens/rotate` 请求正常轮换，保存新 Cookie 并重试原读取一次。
健康检查和业务查询都支持这一步。已部署的 v3 缺少此流程，会把正常轮换误判成
需要登录；应升级网关包，反复扫码或更新 SDK 无法修复。五分钟浏览器挑战期限
与约十分钟令牌轮换间隔均不是实际登录会话期限。真正需要人工授权时重新扫码，
不能把重新认证等同于无交互续期。
Grafana 使用 operator credential_profile 的实例由操作员更新凭据文件，不能混用
交互式 cookie 登录。

## 升级

更新 Observer、Slave HTTP runtime 与 SDK，再发布新版服务镜像/包。历史包的
client 字段仅保留读取和摘要验证，新的发布必须声明 interaction。已有历史包
不能原地改摘要；为新版本创建绑定同一目标资源的新激活，认证并验证后再停旧激活。
业务资源 URI 可以保持不变，新包版本的激活 URI 会随包版本变化。
算力云创建描述符随 suanlicloud-http/v5 更新，引用 suanli-vm-ssh-http/v4。
