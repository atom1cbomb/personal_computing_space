# 能力包统一认证

统一登录从 SDK 0.7 起由 `loom-auth` 提供，适用于 `suanli-vm-ssh-http/v5`、
`kubernetes-http/v6`、`grafana-http/v4` 和 `autopaper-http/v2`。AutoPaper 还使用
SDK 0.8 的直接服务调用，见 [AutoPaper 接入说明](../capability_services/autopaper_http/README.md)。
使用匹配的 Observer 和 HTTP runtime。
`suanli-overleaf-http/v1` 复用同一入口，使用 SDK 0.9.2 及匹配支持点号 Cookie 名和
`pending` 回调的 Observer 与 HTTP runtime；Git token 由激活的 credential profile 提供，
见 [Overleaf 接入说明](../capability_services/suanli_overleaf_http/README.md)。
升级包按新版本发布，保留历史不可变包；重新认证本身不创建新激活。

创建或修改能力包时，参照 skill 中的
[认证接入指南](../skills/personal-computing-space/references/authentication-authoring.md)：
包含系统与能力包职责、manifest、私有 HTTP 接口、实例凭据隔离、过期恢复及验证流程。

## 启动入口

在用户浏览器所在机器安装 SDK 的 `auth` 可选依赖，再安装 Chromium：

```sh
python -m pip install './loom_orchestration-0.10.0-py3-none-any.whl[auth]'
python -m playwright install chromium
python -m loom_orchestration.auth \
  --observer https://observer.example \
  --token-file /private/loom-token \
  --activation grip://loom.local/workspaces/workspace-default/activations/grafana-http/v4/slave-a/bj \
  --activation grip://loom.local/workspaces/workspace-default/activations/kubernetes-http/v6/slave-a/beijing
```

如需指定本地登录档案目录，可追加 `--auth-state-dir /private/loom-auth-profiles`；
临时禁用档案复用可使用 `--no-auth-state`。

也可使用安装后的 `loom-auth` 命令。Observer 使用 HTTP 时显式加 `--allow-http`。
打开 `http://localhost:3000/`，选择实例，点击登录。可重复 `--activation`，URI
必须来自 Observer。客户端只处理选定的激活实例，凭据文件仅在本地进程读取。
SDK 0.9.3 默认在 `~/.loom/auth-profiles/browser` 保存当前用户的私有浏览器档案，所有选定
资源共用它。浏览器跟随实际登录跳转，按 Cookie 域、路径、HTTPS 和同源规则复用
会话，不依赖能力包声明 IdP；上游更换登录提供方后自动遵循新的流程。
持久 Cookie 和站点存储由 Chromium 保存，会话型 Cookie 由客户端在关闭前以 0600
权限原子保存并于下次打开恢复。资源自己的授权确认和服务端验证仍须完成。
不同账号可使用不同的 `--auth-state-dir`；`--no-auth-state` 使用独立的临时上下文。
升级前按 IdP 分散保存的旧档案不自动合并，首次登录建立新档案，旧文件保留。

SDK 0.9.4 的页面提供默认关闭的“自动复用会话登录”开关。开启后依次检查所有
选定资源：已经 ready/authenticated 的直接显示就绪，其余在后台尝试复用会话，
无需逐项点击。无法在自动尝试时限内完成的资源列入“需要处理的资源”，保留
“手动登录”按钮；网络、配置或验证故障显示各自原因，不混同为需要扫码。
手动完成一次登录后，开启中的助手会再尝试其余未完成资源。

自动浏览器等待最多 20 秒，单资源完整尝试最多 60 秒（不含排队），不自动输入
密码或点击上游授权确认。关闭开关停止后续自动尝试，并结束当前自动等待；手动
发起的登录继续。刷新页面保留当前开关状态，重启默认关闭。启动时可显式添加
`--auto-login`；它不能与 `--no-auth-state` 同时使用。

多个登录请求按顺序执行，前一个完成后才为后一个获取最新 revision 并创建挑战，
排队不会消耗挑战有效期。不能同时用两个助手进程打开同一档案；档案被占用或无法
保存时界面明确报错，不静默改用空档案。

首次登录、登录链接过期、关闭窗口、实际凭据失效，都在同一页面点击“重新认证”。
客户端会重新 DESCRIBE 并调用 `auth-start`，无需重启程序或再次激活；启用本地
档案时，会先尝试复用浏览器会话，失败后再要求人工登录。界面只在同一
revision 的激活达到 ready/authenticated 后显示完成。

SDK 0.9.1 分别显示连接、准备远端认证、建立认证连接、打开浏览器和验证阶段，
并显示当前阶段等待秒数。浏览器要等远端挑战建立后才打开；弹窗慢时先检查
页面停在哪个阶段，不要连续重启认证。回调失败、会话失效和连接超时分别显示，
只展示固定错误代码，不展示上游错误正文、回调 URL 或凭据。

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

Cookie 名支持 `overleaf.sid` 这样的点号名称。Cookie 存在不表示用户已登录：
`browser-cookie` 网关在真实上游检查确认当前 Cookie 仍为匿名会话时，可返回
`{"status":"pending"}`（HTTP 200）。运行时和 Observer 保留该挑战的原绑定及
到期时间，统一客户端保持同一浏览器窗口，每秒重新读取并验证 Cookie。
验证成功才返回 `{"status":"accepted"}` 并消费挑战；客户端随后仍等待激活
ready/authenticated。网络故障、权限拒绝或无效响应仍是错误，不能转换成 pending。
`redirect` 回调不接受 pending，成功后的重放和过期挑战仍会被拒绝。
使用此行为须同时更新 Observer、HTTP runtime 和 SDK；能力包需要显式实现
pending 返回值，已有网关不会自动获得该行为。

包声明 `authentication.interaction.mode`：`redirect` 捕获固定本地 OAuth 回调；
`browser-cookie` 从客户端浏览器档案读取目标站点的指定 cookie，并由服务端验证；`device-code`
由原凭据插件完成设备授权。`credential-profile` 用于 Outline、算力云 API 和普通
SSH 的操作员凭据配置，只验证凭据，不发起浏览器授权。登录态复用由客户端管理，
包无需声明 `idp`；历史包中的该字段不再影响复用。
Grafana 保留原站回调，SSH 保留 opkssh 登记的回调。
服务 Cookie 失效只清理目标 Cookie，不清空其他资源或 IdP 的会话。

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

AutoPaper v2 保留 REST API 和浏览器 Cookie 登录。上游后端负责 OAuth token 刷新，
网关保存上游轮换的 Cookie。配套后端补丁区分临时故障（503，保留会话）和凭据
失效（401），并序列化并发刷新；详见
[AutoPaper 自动续期](../capability_services/autopaper_http/README.md#自动续期v2)。
只升级网关不能修复后端已经删除的会话。

算力云 SSH v5 使用 OpenPubkey/opkssh 库实现持久刷新：提前一分钟更新令牌及 SSH
证书，重启复用私有检查点，网络失败退避重试，`invalid_grant` 才要求重新登录。
VM 的 opkssh 过期策略需要支持 `oidc_refreshed`；固定登录时限仍由服务端执行。
详见 [SSH 包说明](../capability_services/suanli_vm_ssh_http/README.md)。

## 升级

更新 Observer、Slave HTTP runtime 与 SDK，再发布新版服务镜像/包。历史包的
client 字段仅保留读取和摘要验证，新的发布必须声明 interaction。已有历史包
不能原地改摘要；为新版本创建绑定同一目标资源的新激活，认证并验证后再停旧激活。
业务资源 URI 可以保持不变，新包版本的激活 URI 会随包版本变化。
算力云创建描述符随 suanlicloud-http/v6 更新，引用 suanli-vm-ssh-http/v5。
