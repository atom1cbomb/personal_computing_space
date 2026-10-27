# AutoPaper 与算力云 SSH 自动续期

## 范围与状态

按用户最终选择，AutoPaper 保留六个 REST 动作及 browser-cookie 登录，不迁移 MCP。
已按用户后续“提交 发布生产”的指示完成上线。生产版本为 autopaper-http/v2、
suanli-vm-ssh-http/v5、suanlicloud-http/v6；算力云创建描述符引用新的 SSH 包。
AutoPaper 后端补丁也已部署。生产验收与回滚信息见下文。

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

最初源码验证时没有可用 Docker CLI。发布阶段使用独立 Docker CLI 接入本机已有
daemon，完成两个网关的真实 Dockerfile 构建及镜像运行检查。
AutoPaper 容器验收额外 **1 passed**，覆盖登录、六个动作、重启、重建、独立卷和故障状态。
Observer 部署前检查与部署配置回归额外 **36 passed**。

## 生产发布

- Loom 实现提交：`30ac8de`，已推送 `origin/main`。
- AutoPaper 后端生产提交：`7f595cf`，已推送 `yzs15/auto-paper` 的 `main`。
  只应用认证相关六个文件；远端预先存在的 embedding Dockerfile、Kubernetes YAML
  及脚本改动均保留。现有环境已安装 filelock 3.25.2。
- 镜像在本机构建，通过现有 registry 的 SSH 转发发布；包由公开 SDK 发布。
  新包均通过生产 `check-package` 后激活于 `slave-a`。

| 包 | 生产镜像 |
| --- | --- |
| autopaper-http/v2 | `localhost:5000/loom/autopaper-gateway@sha256:0d55083a0bbff7d9d9156e312d75571302fb22e91371be8693f684faf1456785` |
| suanli-vm-ssh-http/v5 | `localhost:5000/loom/suanli-vm-ssh-gateway@sha256:53deff19ae409133e50ca50373e9b1055b8f56134c4b7ae8501193f37a6e4fdc` |
| suanlicloud-http/v6 | `localhost:5000/loom/suanlicloud-gateway@sha256:d617b16f32f456c4838a682335005194b057891dc8303fb53455653a5e5d6850` |

生产 Observer 原版本没有 `check-package`，因此同时部署当前源码对应的
`loom-production-core:auth-renewal-20261027`。切换后发现新基础镜像未包含原有
内部服务 CA，已将原 CA 加入专用信任 bundle 并挂载到 certifi 路径，保留原来的
信任关系。Observer 重启后重启 Driver 恢复队列连接。最终健康、execution、
orchestration 均正常，既有资源身份保留。

用户通过统一 `loom-auth` 门户分别完成了新包首次登录；未复制旧包凭据。
目标 VM `9.0.16.13` 的同一 OIDC 提供方策略由 `24h` 改为 `oidc_refreshed`，
原文件备份为 `/etc/opk/providers.auth-renewal-20261027.bak`。

## 生产验收

- AutoPaper v2 六个动作全部通过：95 个会议目录、37 个已存储会议、ACL 2025
  共 3379 篇论文、论文详情及搜索。最终搜索 Run `run-06ae869ff9c7` completed，
  返回 total=46。
- SSH v5 使用新登录凭据完成真实远端执行；切换 `oidc_refreshed` 后再次执行通过。
  私有 checkpoint 权限 0600，已持久化 refresh token，worker 状态 authenticated。
- AutoPaper v2 和 SSH v5 均通过公开 stop/activate 重启：revision 1 → 3，
  不再次登录即恢复 ready/authenticated，且恢复后的读取/SSH 执行通过。
- 算力云 v6 的真实实例列表调用通过。
- 旧 AutoPaper v1、SSH v4、算力云 v5 在对应新实例验证通过后停用，旧镜像和
  持久卷保留。

首次搜索发现既有 embedding 访问通道停在 configuration_error，记录的故障时间
为当天 12:53 CST，早于本次发布。通过现有公开 `services-ensure` 重建连接后，
通道 ready、模型 health 正常，随后搜索验收通过。未修改模型工作负载或节点 TLS 入口。

真实账号已验证登录及重启恢复；本次未等待完整的一小时 ID token 生命周期，
不把实账号下一轮后台刷新计入已完成的实测。刷新轮换、撤销和错误恢复由前述
Go/后端测试覆盖。

## 运维记录与回滚

私有操作记录保存在 `production/auth-renewal-20261027/`，包含公开检查结果、
镜像构建日志、最终业务结果和重启验证。生产 Observer 的原 compose、数据库
备份和镜像信息保存在 `/opt/loom-production/releases/auth-renewal-20261027/`；
当前 compose 指向本次源码快照，并保留已有 CA 信任挂载。后端部署没有数据库迁移。

需要回滚时可恢复原 Observer compose/镜像并重连 Driver，重新激活保留的旧能力包；
SSH 回滚同时恢复已备份的 provider policy。后端只回退认证提交，保留其他未提交改动。
