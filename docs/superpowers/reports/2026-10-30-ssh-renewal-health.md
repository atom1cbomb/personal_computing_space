# SSH 续期修复与验证

## 问题与修复

两台 VM 的 SSH 接入在首次后台刷新后进入待认证。固定版本的 OpenPubkey
刷新校验默认要求空 nonce，而实际 IdP 刷新响应保留首次登录的 nonce。
同时，交换成功后若校验失败，旧实现丢弃已经轮换的新 refresh token，重试时
再次使用已消费的旧 token，随后收到 `invalid_grant`。

刷新交换与校验现在分离：先将响应写入私有 checkpoint，再验证签名、issuer、
audience、身份、时效、access-token hash；nonce 允许省略，存在时须匹配首次
登录值。校验暂时失败或进程重启后复用 checkpoint，避免重复消费旧 token。
验证成功后才发布 SSH 证书。新增有界、0600 权限的结构化事件记录，仅保留
阶段、时间和允许的错误码，不记录原始错误或凭据。

网关独立探测实际 SSH 权限；续期失败但服务器仍接受当前凭据时保持可用。
退出的 worker 会被回收。生产默认在 token 到期前一分钟续期；仅隔离测试镜像
通过 `SSH_REFRESH_INTERVAL=60s` 缩短调度间隔，不改变 IdP token 的有效期。

发布模板升级为 `suanli-vm-ssh-http/v6`，算力云 `v7` 创建描述符引用该版本。

## 真实账号加速验证

独立 worktree：`.worktrees/ssh-renewal-health`，分支 `fix/ssh-renewal-health`。
测试包 `suanli-vm-ssh-http/v6-soak-20261030` 使用独立私有卷，通过统一门户
完成登录后关闭本次门户，避免自动重新登录掩盖续期故障。

UTC 2026-10-30 09:38:24–09:48:54，实际验证约 630 秒：

- `npc-test` 对应测试接入完成 11 次续期，另一台 VM 完成 10 次。
- 18 个真实 SSH Run 成功，16 次 readiness 采样通过。
- 两实例首次登录时间保持不变，刷新 nonce 均存在且匹配，续期错误为 0。
- 临时接入最终停用，18 个 Run 已关闭。

证据位于 gitignored `production/releases/2026-10-30-ssh-renewal-soak/`。
`summary.json` 区分验证窗口内续期次数与清理时累计次数，不将额外等待计入验收。

Go `go test -race ./...` 覆盖真实签名 OIDC 响应、nonce 正反例、身份/audience/
expiry/签名/hash 拒绝、refresh token 轮换与重启恢复，以及日志不泄露凭据。
Python 回归覆盖网关认证、SSH 执行与 GRIP 生命周期。

发布前完整定向回归：`tests/capability_services`、
`tests/slave/test_container_http_runtime.py`、`tests/slave/test_runtime_plugins.py`
共 **401 passed、2 skipped**；Go race、契约生成 `--check` 和 `git diff --check`
通过。实现提交 `4333ffd`，合并到主目录的提交 `19cc130`，保留此前主分支上的
生产任务验收文档提交。

## 生产切换

从合并后的主目录构建生产镜像，恢复默认 `SSH_REFRESH_INTERVAL=0s`。
镜像在本机完成构建，经 SSH 传输到现有 registry 发布，再由 Slave 按不可变摘要拉取。

| 包 | 镜像 |
| --- | --- |
| `suanli-vm-ssh-http/v6` | `localhost:5000/loom/suanli-vm-ssh-gateway@sha256:ab0aa1b64ef285325d86ecffbee25d186e1e995054f92096dfe176069876d90f` |
| `suanlicloud-http/v7` | 复用 v6 网关镜像，仅更新创建描述符中的 SSH 包引用 |

公开 `check-package` 均通过。两个 SSH 实例以原实例名、原 VM target 创建独立
v6 接入，通过统一门户完成首次登录，不复制旧凭据。算力云 v7 使用运维历史部署
记录中明确选择的 `suanlicloud-prod` profile。三条已有 VM 记录的管理接入标签
同步指向 v7，其余属性保留。旧 SSH v5 两实例和旧算力云 v6 在新实例验收后停用，
旧包、镜像和私有卷保留。

UTC 10:12:30–10:18:50 生产验收结果：

- 9 个业务 Run 全部成功并关闭：两台 SSH 首次执行、两次重启后执行、两次通过
  VM 资源入口执行、两次最终执行，以及一次真实算力云实例查询。
- SSH 实例通过公开 stop/activate 完成 revision 1 → 3，首次登录时间不变。
  本次登录辅助进程在重启前关闭，确认恢复不依赖自动重新登录。
- 两实例的生产配置均为 `0s`，checkpoint 权限 0600；最终无 pending 响应，
  worker 状态 authenticated。两个 SSH 接入与算力云 v7 均 ready/authenticated。
- 切换后约 90 秒内完成 8 次 readiness 检查；VM 公开入口仅列出对应 v6 接入。
- 公开诊断源码与合并后的主目录逐文件一致。

资源入口验收最初误用了仅解析 activation `metadata.actions` 的 SDK 快捷函数；
随后按资源实际 offers、输入 schema 和关联 activation revision 直接调用，验证
真实 Run 结果。脚本还按结果 schema 保留 `truncated` 字段。两次脚本适配错误及
后续成功结果均保存在私有日志中，没有修改 SDK 或服务端协议来绕过检查。

生产证据在 gitignored `production/releases/2026-10-30-ssh-renewal-production/`，
最终 `verification.json` 为 passed。本轮生产验收未等待完整一小时 token 生命周期；
连续真实续期证据来自前述一分钟调度、十分钟隔离验证。

另通过两个只读 SSH Run 核对服务器策略并关闭 Run：认证测试 VM 使用
`oidc_refreshed`，`npc-test` 仍使用 `24h`。本次未修改服务器授权策略；后者仍受
原登录的 24 小时限制，客户端续期修复不能延长服务器明确规定的授权期限。
这两次检查记录在 `policies.json`，不计入上述 9 个验收 Run。
