# 普通 VM SSH 使用说明

这个包为已登记的 SSH VM 提供 `exec` action。创建 activation 时绑定目标 VM，并在
`instance_config` 中传入 `host`、`port`、`user`；通过 activation 的 credential profile
提供 `id_ed25519`。这些连接信息和凭据不会进入业务输入或公开 attributes。

等待 activation ready 后调用 `exec`，输入为 `{command, timeout_seconds}`。命令通过 SSH
stdin 运行 `sh -s`；超时时间为 1–3600 秒，默认 300 秒，命令最多 1 MiB，每个输出流
最多 16 MiB。成功以 action 声明的 validator 为准：退出码必须为零且不能超时。

服务会为每个 activation 保存独立 `known_hosts`，首次连接接受新 host key，后续检测
host key 变化。业务请求不能选择其他目标、SSH options 或私钥路径；认证失败时重新验证
原 activation。
