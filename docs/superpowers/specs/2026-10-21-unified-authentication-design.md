# 统一能力包认证与重新认证

日期：2026-10-21。遵循 [spec 设计原则](../spec-design-guidance.md)。

## 目标与现状

用户通过一个本地入口为 Kubernetes、算力云 SSH 和 Grafana 激活实例登录，
登录入口失效、窗口关闭、认证失败或实际凭据过期后，都能在同一入口再次认证。
同一激活实例、目标资源 URI、配置、持久卷和 activation revision 保持不变。

当前三个包已有 start/status HTTP hook，SSH 和 Grafana 已共用 Observer 的
一次性 BrowserAuth bridge；但各自携带本地脚本，返回结构不同，脚本不能自行
重新发起认证。SSH 登录入口超时被当成认证故障；Kubernetes 的成功探测进程
被永久缓存。Observer 的 failed 状态只允许 failed/stopped，导致显式 auth-start
也无法恢复。此次替换旧客户端和认证声明，不保留旧接口兼容分支。
本设计需要保持向后兼容的唯一部分是历史不可变包的读取、摘要验证及运行：
旧 client 声明只作为历史数据保留，不再执行，新的发布必须声明 interaction。
历史包的统一交互需要发布新版；不能原地改写历史摘要。

## 统一契约

继续使用激活资源的 GRIP auth-start，输入 expected_revision。响应统一为
resource、revision、authentication。authentication 包含 status、method、
可用的 verification_uri、user_code、expires_at、poll_after_seconds。
需要浏览器桥接时，还返回仅供本地客户端使用的 client（bridge_url、session_id、
session_token）。它们只在认证响应和内存中传递，不进入资源描述、Run 或内容存储。
不返回整个 package 或内部 health report。

包的 body.authentication 声明 start_path、status_path、interaction：

| interaction.mode | 行为 | 当前使用者 |
|---|---|---|
| redirect | 隔离浏览器打开授权站点，捕获固定 localhost 回调的 code/state，交给 bridge | suanli-vm-ssh-http |
| browser-cookie | 隔离浏览器完成原站 OAuth，读取该 origin 的指定 cookie，经 bridge 验证并保存 | grafana-http |
| device-code | 展示授权地址与设备码，打开浏览器，由服务端凭据插件完成授权 | kubernetes-http |
| credential-profile | 验证操作员配置的凭据，缺失/失效时提示更新配置 | outline-http、suanlicloud-http、vm-ssh-http |

前两种必须声明 browser_path；browser-cookie 额外声明 cookie_name。客户端不
执行包提供的 Python entrypoint，也不根据包名分支。Grafana 保留原站 OAuth
回调；SSH 使用已有 localhost 回调。凭据仍由各激活实例独立保管。

DESCRIBE 的 metadata.authentication 暴露 method、status 和允许公开的挑战
信息，供统一客户端观察；不复制 runtime_profile、instance_config 或 bridge token。
status 尚未探测时为 unknown，客户端不能把它当作认证成功。

## 一个客户端

在独立 loom-orchestration SDK 中提供 loom-auth CLI（也支持 python -m
loom_orchestration.auth）。输入 Observer、token-file 和一个或多个激活 URI。
复用 LoomClient、httpx 和现有 Playwright；Playwright 是 SDK 的 auth 可选依赖。
技能归档只携带 SDK wheel，删除能力包专用客户端及其打包逻辑。

入口页绑定 loopback，展示每个激活的状态和“登录 / 重新认证”。每次点击通过
DESCRIBE 获取当前 revision，再调用 auth-start；不需要代理写临时会话文件、
重启本地程序或重新激活。请求使用同源校验与随机 CSRF token，Observer 凭据
仅在本地进程中读取。进行中的同一实例不重复启动；不同实例互不覆盖。

浏览器使用客户端自己创建的隔离上下文。redirect 的 localhost 回调由浏览器
路由和导航请求事件捕获，转交同一会话的 bridge，不读取其他浏览器配置。browser-cookie 只
采集 bridge 返回的 HTTPS origin 上声明的 cookie。回调被接收后继续轮询同一
激活，只有 ready 且 authenticated 才显示完成；不能以拿到 cookie/回调代替验证。
窗口关闭、挑战过期、上游拒绝等显示可重试状态，异常正文不包含凭据。

## 过期与状态恢复

登录挑战过期与实际凭据过期分别处理。Observer 的浏览器挑战仍有效五分钟，
新的 auth-start 替换同实例旧会话。旧 token、旧 callback 和旧 revision 均被拒绝。
挑战的时间不能当作 SSH/Grafana/Kubernetes 凭据的有效期。

SSH 登录挑战超时恢复 auth_required；SSH 探测中缺少凭据或 Permission denied
返回 auth_required，网络、配置和程序故障保留 auth_failed。Grafana 继续以实际
API 探测识别失效，并保留已有 cookie 轮换。Kubernetes 每隔短周期重新探测权限，
让现有 kubelogin 插件自行刷新 token；已退出的设备码进程不再返回旧登录链接，
显式重新认证启动新进程。权限不足和连接故障不宣称登录成功。

运行中的认证故障表现为 degraded，等待认证表现为 pending_auth。对于已经处于
failed 的旧实例，只有 Observer 显式 auth-start 的可信返回可以恢复到
pending_auth/ready/degraded；普通健康上报不能解除 failed。此恢复仍校验包摘要、
实例、desired_state 和 revision。容器不存在或未运行时不能通过认证恢复。

## 实施与验收

1. 修改包契约、HTTP runtime、Observer 返回与描述、显式认证恢复路径。
2. 新增统一 SDK 客户端，删除两个旧客户端；更新所有声明认证的包、文档、技能和归档。
3. 修复三个网关的过期/故障语义及 Kubernetes 定期凭据验证。
4. 回归验证三种登录方式、登录失败后原入口重试、多实例隔离、五分钟入口过期、
   凭据失效后同实例恢复、failed 显式恢复与普通报告限制、旧回调/revision 拒绝、
   认证信息脱敏和只在实际 ready 后显示成功。
5. 运行受影响测试和仓库非外部依赖测试，验证 SDK wheel/技能归档与实际浏览器页面。
   此次修改完成后报告本地验证结果；生产端需要同步升级 Observer、runtime 和新的
   不可变包版本，不能用更新客户端代替生产升级。真实账号扫码仍由用户完成。

## 生产验证补充：凭据重检不应导致 readiness 抖动

2026-10-21 部署验证发现，Kubernetes v5 在每次定期 can-i 检查启动后立即返回
无挑战的 auth_required，造成有效凭据的实例在 ready/pending_auth 之间切换。
v6 保留上一次已验证成功的状态，直到本轮检查得到结果；宽限仅覆盖正在运行的
定期检查，最长为网关命令超时（默认 30 秒）。出现设备授权挑战或检查失败时立即
退出宽限；超时为 auth_failed，可显式重新认证。首次登录和显式 auth-start 不沿用
缓存成功。业务 kubectl 请求仍由真实 API 校验权限，宽限不绕过集群认证。
