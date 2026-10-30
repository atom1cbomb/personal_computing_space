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
