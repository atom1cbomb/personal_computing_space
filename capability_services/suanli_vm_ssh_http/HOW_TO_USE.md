# 算礼云 VM SSH 使用说明

这个包为已登记的 VM 提供远程 shell。activation 必须绑定目标 VM，并在
`instance_config` 中提供 `host`、`port`、`user`；SSH 私钥和算礼云登录状态由
operator credential profile 管理，不能放入配置或 action 参数。使用 `suanli-vm-ssh-http/v5`
时通过统一登录入口完成认证，等待 activation 达到 `authenticated`/`ready` 后再执行。

唯一业务 action 是 `exec`：输入 `{command, timeout_seconds}`，命令通过 SSH stdin
交给 `sh -s`。`timeout_seconds` 为 1–3600 秒，默认 300 秒；命令最多 1 MiB，每个
输出流最多保留 16 MiB。成功必须同时满足 `exit_code == 0` 和 `timed_out == false`，
以 action 声明的 validator 为准。

目标、SSH options、私钥路径和 host key 不能由业务请求覆盖。调用前读取 activation
的当前 `exec` offer 和 schema；认证失效时重新认证原 activation，不要复制私钥或创建
第二个绑定相同 VM 的实例。
