# 外部服务浏览器登录与远程回调代理

认证客户端与重试行为已由 [统一认证设计](2026-10-21-unified-authentication-design.md) 更新；下文保留原设计依据。

状态：本地浏览器助手桥接已实现；真实 OIDC/Docker E2E 仍需按部署环境验收。遵循
[设计规范](../spec-design-guidance.md)。

本设计补充 [命名实例与 VM 访问](2026-09-21-named-activations-and-suanli-vm-access-design.md) 的认证部分，复用 [通用外部认证](2026-09-19-external-service-authentication-design.md)。不以当前单元测试通过作为该链路已实现的证据。

## 1. 决策及依据

使用官方 opkssh v0.14.0 的 localhost listener，不修改 opkssh、不实现 OIDC/OpenPubkey，也不新增 SSH runtime。用户运行随能力包携带的本地浏览器助手；VM 访问容器仍不发布 host port。

已核对官方源码：

- [opkssh ProviderConfig](https://github.com/openpubkey/opkssh/blob/v0.14.0/commands/config/providerconfig.go) 将远程 redirect URI 交给 OpenPubkey。
- [OpenPubkey StandardOp](https://github.com/openpubkey/openpubkey/blob/v0.23.0/providers/standard_provider.go) 仍监听 localhost；`/login` 生成授权跳转和 Cookie，`/login-callback` 处理授权码。`RemoteRedirectURI` 只替换发给 IdP 的 redirect URI。
- [OpenPubkey localhost listener](https://github.com/openpubkey/openpubkey/blob/v0.23.0/providers/utils.go) 限制本地监听地址。不能通过修改该配置直接监听公网。

因此必须代理 **登录入口与回调两段**，并保留同一次登录的 Cookie。只转发授权码或返回 `callback_received` 不会完成认证。

本设计使用一次性的本地助手桥接，不要求用户拥有 Observer 主机账号，也不要求为每个
外部 IdP 注册新的 Observer callback。用户本机的 `localhost:3000` 仍是已登记的
redirect；助手仅把本次 `/login` 与 `/login-callback` 转发到 Observer 的短期 bridge，
再由 Observer → Slave → runtime → VM 能力包容器完成认证。Observer 通过固定 Worker
auth 路径连接 Slave，不经过 Driver，也不把容器端口暴露到公网。

```text
浏览器 → 本机 localhost:3000 → 本地助手 HTTPS bridge → Observer → Slave → container-http runtime
                                                  ↓
                               指定 VM 访问容器 → localhost opkssh
浏览器 ← 授权跳转 ←─────────────────────────────────┘
浏览器 → IdP 登录 → 本机 localhost:3000/login-callback → 同一实例的 opkssh
```

Observer/Slave 只处理通用认证消息；issuer、client_id、localhost 路径、Cookie 和 opkssh
进程全部由访问包管理。Driver 只提交控制意图，不在生产认证数据面出现。不提供任意
URL、端口、HTTP 方法或路径代理。

## 2. 最小协议扩展

包的 `body.authentication` 保留 `start_path`、`status_path`，为需要浏览器往返的服务增加可选 `browser_path`：

```json
{
  "authentication": {
    "start_path": "/v1/auth/start",
    "status_path": "/v1/auth/status",
    "browser_path": "/v1/auth/browser",
    "client": {
      "kind": "local-browser-bridge",
      "protocol_version": "1",
      "entrypoint": "client/loom_vm_auth.py"
    }
  }
}
```

三者是互不冲突的包内 origin path，不能与健康或业务 export 路径重叠。`browser_path` 不导出为业务能力，不需要 descriptor 或 IoContract。未声明它的设备码、静态凭据包继续使用现有协议，这不是旧浏览器方案的兼容层。

有 `browser_path` 的包必须同时声明 `authentication.client`，其中的客户端入口随能力包
目录交付，不放进 Slave 容器。安装和普通健康探测只报告 `auth_required`，不自动创建
无限重试的登录进程。用户通过现有 Observer `auth/start` 为一个确定的命名实例发起
登录。该请求必须指定精确包 digest、Slave 和实例名。

Observer 创建随机 `session_id`，并把 `{session_id, callback_uri, expires_at}` 沿既有 `auth_start` 通道传给包。这是一次临时登录尝试的关联信息，不是新的 activation ID。`callback_uri` 固定为已经登记的 `http://localhost:3000/login-callback`，不接受调用者输入。

包内 `browser_path` 接收固定 JSON 消息：

| action | 输入 | 返回 |
| --- | --- | --- |
| `open` | `session_id` | `{status: "redirect", location}` |
| `callback` | `session_id`, `query`（有界键值对列表） | `{status: "accepted"}` 或固定错误码 |

`query` 保留授权回调参数的值与重复项；包按其官方客户端要求校验参数，不能在代理中截断、替换 OIDC state 或构造 token。响应不允许任意 HTML 或任意响应头，避免将能力容器变成 Observer 同源页面服务器。

新增通用 `auth_browser` 调用贯穿 Observer、WorkerSession、Slave、runtime-plugin
Host/process/server 和 container-http runtime；生产请求由 Observer 直接中继到 Slave，
仍使用现有内部鉴权、workspace 和 activation revision 校验。采用专用瞬态 HTTP 调用，
不走会持久化 arguments/response 的 Driver command receipt、Run、ContentStore 或事件链路。

## 3. 对外入口与浏览器绑定

能力包的 `authentication.client` 声明本地助手入口和协议版本。`auth/start` 返回
Observer bridge URL、session id、短期 session token 和客户端元数据。用户在自己的
电脑运行助手；助手只绑定本机 loopback，不要求 Observer 主机 SSH/TCP 访问权限。

浏览器和 IdP 继续使用已有的：

```text
http://localhost:3000/login-callback
```

无需为每个包、Slave 或 VM 注册 URI，也不依赖 IdP 接受通配符。

Observer bridge 只接受固定的 `POST /api/v1/auth/browser/bridge`，由 bearer token
绑定到一个 package digest、Slave、实例和 activation revision；它不接受 upstream、
path、Host 或任意 header。

助手打开本地入口时：

1. 校验该登录尝试的 workspace 与当前用户可访问的 workspace 相同。
2. 生成独立的随机浏览器绑定值并保存到临时会话；助手随后将它写入
   `HttpOnly; SameSite=Lax`、host-only Cookie。因为已登记 callback 是 localhost HTTP，
   不能设置 `Secure`；有效期不超过该 session。
3. Observer 将绑定值返回给助手；助手把它作为 localhost host-only Cookie 写给浏览器。
4. 助手调用 `open`，收到包校验后的授权 Location 后返回 302；不自动跟随跳转。

localhost callback 不携带 Loom API token。助手用短时 bearer token 找到实例，再代理回调。
缺失 token、过期、已消费或找不到记录时直接拒绝；不能退回按包名、IP、query 中的
Slave 或“第一个 pending 实例”选择。

首版同一浏览器 Cookie jar 只允许一个进行中的浏览器认证；打开第二个实例的登录入口返回 `browser_auth_in_progress`，不能覆盖第一条绑定。不同浏览器会话可以同时登录不同 VM。需要另开浏览器配置文件或先结束原认证，不增加多标签页关联协议。

回调仅支持授权码流程的顶层 GET/query 模式，使用 SameSite=Lax；不支持 `form_post` 或 iframe 登录。显式重登让原 session 失效，原浏览器 Cookie 不再可用。

生产必须提供受信任的 HTTPS Observer 入口和 workspace 用户访问控制。仅有内部 service
token 不等于浏览器用户鉴权；bridge token 只能由已授权的 `auth/start` 响应发放，不能
单独创建或延长登录会话。

## 4. 会话状态与实例校验

Observer 只在内存维护有数量上限、默认 5 分钟 TTL 的临时记录：

```text
session_id → workspace, package_version_ref, package_digest,
             target_slave, instance_name, activation_revision,
             bridge_token, expires_at, browser_binding, phase
```

`phase` 只需区分已创建、浏览器已绑定、回调处理中；完成、失败和到期即删除。每个实例同时只允许一个登录尝试。每次回调都核对当前 activation 仍为 running、revision 与包/实例身份一致。Slave 检查本地 activation，runtime 验证完整容器 labels，不让旧回调命中新实例。

这是短时回调路由状态，不是新的持久资源表，不生成 digest。Observer 重启后返回会话失效，用户重新发起登录；不为五分钟交互增加跨进程恢复系统。Driver 重启不影响已建立的
Observer→Slave 认证数据面，但新的控制请求仍须使用当前 Driver epoch。Slave/容器失去
本次登录进程时，该 session 失败，不能自动换一个进程继续消费旧 callback。

Session 创建、回调占用和终结按实例/会话加锁。回调一旦开始向下游发送便不可自动重放；若响应丢失，查询认证状态确认结果，不再次交换授权码。无效的输入在转发前拒绝；已经提交的失败回调结束本次尝试。

认证进程仍由包管理：显式重新登录终止并回收旧进程、关闭旧 Cookie jar、作废旧 session，保留 `known_hosts`。包独立执行 TTL 清理，即使 Observer 重启也不遗留登录进程。

## 5. VM 访问包如何代理 localhost

VM 包读取固定的 Suanli issuer/client_id/scopes 配置。用官方 opkssh 配置将本地 redirect URI 固定为 `http://localhost:3000/login-callback`，每台 VM 一个容器，不需要探测三个候选端口。启动参数包括：

```text
opkssh login --disable-browser-open
  --config-path <镜像内固定 opkssh 配置>
  --private-key-file <该实例的私钥文件>
```

配置来自镜像/受控实例认证上下文，不靠 runtime 任意环境变量注入；VM IP 仍只来自 `/run/loom/instance.json`。本地 listener 只允许本容器访问。

包为当前 session 使用一个 `httpx.Client` Cookie jar，`trust_env=False`、`follow_redirects=False`，仅访问固定 localhost 地址。`open` 等待 listener 有界就绪后请求 `/login`，保存官方客户端的 Set-Cookie，并返回授权 Location。只允许匹配配置 issuer 经 TLS discovery 获得的 authorization endpoint（scheme/host/port/path）；保留官方生成的 query，包括 state、nonce 和 PKCE。发现不一致直接失败。

`callback` 用同一 Cookie jar 向固定 `/login-callback` 转发参数；Cookie 留在包的内存，不向 Observer 暴露。opkssh 自己完成 state/nonce/PKCE 与 token 校验，换取并写入 SSH 凭据。代理不替换这些校验，也不自行处理 OIDC token。不同 session 不共享 Cookie jar。

包不得因“私钥文件存在”报告 authenticated。必须观察本次登录进程正常退出，再用新凭据对固定 VM 执行有界、无副作用的 SSH 探测。认证完成但 VM 网络暂未就绪时保持不可调度并有限等待；超时或 host key 变化报告明确错误，通过现有 health/inspect 链路使实例 degraded。认证状态与资源就绪不能混为一谈。

浏览器 callback 成功只展示“认证回调已接收，请查看实例状态”，不直接将 activation 改为 ready。只有包的后续权限/SSH 探测成功，才能沿既有 health report 链路进入 ready。过期凭据需要重新授权，不因私钥还在而继续声称可用。

## 6. 安全与部署边界

- `code`、state、PKCE Cookie、token、私钥都不进入数据库、ContentStore、健康报告或审计 payload。只记录实例、阶段、时间和固定错误码。
- 外层 TLS 代理、ASGI/Uvicorn、HTTP 客户端与异常处理均关闭这些认证端点的 query/body 日志；不是只在业务 logger 脱敏。返回 `Cache-Control: no-store`、`Referrer-Policy: no-referrer`，最终跳转到无 query 的固定结果页面；不加载第三方资源。
- 内部鉴权凭据不发给浏览器或能力容器。生产跨主机链路若不在受信任隔离网络，必须加 TLS；不让授权码和内部 token 裸奔。
- 包的 localhost 地址、容器地址和 callback URI 由服务端确定。请求不能指定 upstream、port、Host、任意 path 或附加 header；不增加通用反向代理配置 DSL。
- 官方 opkssh Cookie 只在容器内往返，不做浏览器 cookie 域/路径重写。新增的 Observer Cookie 只承担浏览器到实例的绑定，不声称替代官方 OIDC 安全校验。
- SSH 入口、workspace 访问控制和内部链路隔离必须由 operator 确认。OIDC discovery 存在设备码 endpoint 并不能证明本 client 允许设备码。

## 7. 实施范围与遗留收敛

1. 补齐命名实例的已有缺口：Worker `auth_start` 参数、RemoteRepository 目标/配置透传、具名执行与停止隔离。回调建立在这条身份链路上，不新增第二套实例 registry。
2. 扩展 `authentication.browser_path` 契约与 runtime-plugin 的 `auth_start` 上下文、`auth_browser` 通道。设备码包不要求实现 `auth_browser`。
3. Observer 实现固定用途 bridge、短时会话与绑定；Slave 通过固定 Worker auth 路径
执行容器转发。能力包目录携带本地助手，不引入独立 tunnel listener。
4. VM 包实现官方 `/login` 与 `/login-callback` 的局部代理、认证进程清理及实际 SSH 就绪探测。
5. 删除当前占位 `/v1/auth/callback`、抓取任意日志 URL、私钥存在即 authenticated 的代码。删除并修正 README 中“设置环境变量即可完成远程登录”的说明。协议仍用 `auth_required/authenticated/auth_failed`，不用未被 runtime 接受的 `auth_pending`。
6. 发布本地助手与真实镜像 digest 和契约，执行生产验收。不能把全零 image/descriptor
digest 示例发布为可用包。

不新增认证数据库、认证 broker、通用 HTML 代理或特定云厂商的 Observer/Driver 分支。UI 复用 verification_uri，不需要新建 VM 登录页面产品。

## 8. 验收与生产前置条件

先完成自动测试：

- Mock localhost 服务必须设置并验证 Cookie；证明 `open → callback` 保持 Cookie、未携带 Cookie 失败，且不跟随授权跳转。
- 用固定版本官方 opkssh 和测试 OIDC 服务验证真实 state、nonce、PKCE、code exchange 与凭据写入；仅 mock HTTP 不能替代此项。
- Observer → Slave → runtime → 两个独立 VM 容器的 Docker 集成；两个浏览器会话不串实例，
  同浏览器并发明确拒绝。
- 过期、重登、Observer 重启、容器重建、deprovision、旧 revision、重复 callback 都不能操作新实例；回调响应丢失不自动重试 code exchange。
- 登录成功但 SSH 不通或 host key 改变不进入 ready；私钥文件存在但登录未完成/已过期也不进入 ready。
- 日志、DB events、健康报告和 ContentStore 检查不到授权码、Cookie 或私钥。测试中用唯一假值验证，包括 TLS 入口访问日志。

生产验收依赖 operator 提供受信任的 HTTPS Observer 入口和用户访问控制；继续使用已
登记的 localhost redirect，不需要为每个 IdP 新增 callback。若用户环境不能运行本地
助手，才另行启用固定 HTTPS callback 并满足对应 IdP 注册要求。

前置条件通过后，按主 spec 的预算/授权创建测试 VM，Observer 返回登录链接，用户登录，等待该实例 ready，从 Observer 发起唯一标记的 exec 任务，核验退出码/输出及内容寻址取回。最后按约定终止测试 VM、停用访问实例并清理专属状态。前置条件不满足时，不创建付费资源来等待认证接入。
