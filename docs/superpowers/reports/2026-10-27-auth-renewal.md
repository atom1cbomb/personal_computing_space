# AutoPaper 与算力云 SSH 自动续期

## 范围与状态

按用户最终选择，AutoPaper 保留六个 REST 动作及 browser-cookie 登录，不迁移 MCP。
源码模板为 autopaper-http/v2、suanli-vm-ssh-http/v5；suanlicloud-http/v6 的创建
描述符引用新的 SSH 包。本次仅完成源码、配套上游补丁和本地验证，未发布镜像、
激活新版本、修改远端 SSH 策略或重启生产服务。

## 修复

AutoPaper 网关将成功响应中的有效同源会话 Cookie 持久化，并阻止旧请求恢复或覆盖
重新登录后的凭据。短暂写入故障保留新 Cookie 于内存，后续调用重试保存。
网络/权限/上游服务错误保留认证状态文件，搜索不自动重放。

AutoPaper 后端本来就会刷新 OAuth token；实际缺陷是任何刷新异常都会删除会话，且
并发请求可能重复消耗轮换中的 refresh token。修复在独立检出
`/root/auto-papers-auth-refresh` 完成，基于
`99cc56c340552a47e08fe8e7fecae9ba72a6b064`。可移植补丁保存在
`capability_services/autopaper_http/upstream/auth-refresh.patch`，含依赖、实现及测试。
暂时故障返回 503 并保留会话；明确 invalid_grant 才删除会话。按会话分组的文件锁
序列化同一主机各进程的刷新，锁内重新读取数据库。多主机部署需要共享锁或数据库锁。
已经删除的会话无法由此补丁恢复。

SSH 使用固定版本的 OpenPubkey 和 opkssh 库处理登录、验证及证书生成；新增 worker
在 ID token 到期前一分钟刷新。私钥、原 PK token、刷新后的 ID token 和 refresh token
在激活私有卷以 0600 权限原子保存。刷新后的 ID token 必须单独保存，因为上游
PKToken 的 JSON 序列化不包含它。重启恢复后重建证书；网络故障退避重试，明确撤销
才要求重新认证。网关仍执行真实 SSH 探测，登录完成后不等待常驻刷新进程退出。

SSH 服务端的 provider policy 需要支持 `oidc_refreshed`。固定 24h/oidc 登录时限
仍然生效，客户端不会改变 VM 的权限策略。包摘要升级隔离凭据，新版本首次需要登录。

## 验证

- Loom：`tests/capability_services`、`tests/slave/test_container_http_runtime.py`、
  `tests/slave/test_runtime_plugins.py`，**351 passed，1 skipped**。
- 最后补充私有域名的 host-only Cookie 轮换校验后，AutoPaper 网关和 GRIP 认证链路
  定向回归 **60 passed**。
- 上游 AutoPaper：`tests/test_auth_refresh.py`、`tests/test_api/test_auth.py`，
  **23 passed**。覆盖网络恢复、真正撤销、异常 token 响应、持久化、线程/进程并发。
- SSH worker：`go test -race ./...` 通过。覆盖 refresh token 轮换/省略、身份验证失败、
  刷新 ID token 的证书编码与重启恢复、网络故障重试、撤销和状态文件内容。
- 后端补丁在干净的基线文件上通过 `git apply --check`，应用结果与测试过的文件逐字节一致。
- 契约生成 `--check`、Python compileall、`git diff --check` 通过。

Docker 验收跳过：当前 WSL 环境没有可用 Docker daemon。SSH worker 已完成
`CGO_ENABLED=0 go build -trimpath`，但未
构建/运行新容器镜像，也未进行真实账号 OAuth 刷新或真实 SSH 服务端续期验收。
