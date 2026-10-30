# 算礼云 VM SSH 使用说明

这个包为已登记的 VM 提供远程 shell。activation 必须绑定目标 VM，并在
`instance_config` 中提供 `host`、`port`、`user`；SSH 私钥和算礼云登录状态保存在
每个激活独立的私有持久卷，不能放入配置或 action 参数。使用此包时通过统一登录入口完成认证，等待 activation 达到 `authenticated`/`ready` 后再执行。

命令执行业务 action 是 `exec`：输入 `{command, timeout_seconds}`，命令通过 SSH stdin
交给 `sh -s`。`timeout_seconds` 为 1–3600 秒，默认 300 秒；命令最多 1 MiB，每个
输出流最多保留 16 MiB。成功必须同时满足 `exit_code == 0` 和 `timed_out == false`，
以 action 声明的 validator 为准。

目标、SSH options、私钥路径和 host key 不能由业务请求覆盖。调用前读取 activation
的当前 `exec` offer 和 schema；认证失效时重新认证原 activation，不要复制私钥或创建
第二个绑定相同 VM 的实例。

## 流式文件传输

`files.upload`、`files.download` 是 byte-stream 动作，复用本 activation 的 SSH
认证、目标与 known_hosts。目标 VM 须安装 Python 3。文件以 64 KiB 分块沿 GRIP/SSH
传输，不创建 Run，不上传到 ContentStore，不受 exec 输出大小上限约束。

## Python SDK

安装 `loom-orchestration 0.12.0` 后，使用发现所得的 activation URI：

```python
from loom_orchestration import LoomClient, VmFileClient

async with LoomClient(observer_url, token=token) as client:
    files = await VmFileClient.discover(client, activation_uri)
    uploaded = await files.upload("./input.bin", "/home/worker/input.bin")
    downloaded = await files.download("/home/worker/result.bin", "./result.bin")
    print(downloaded["size"], downloaded["sha256"])
```

`overwrite=True` 显式允许替换目标文件。SDK 不自动重试上传；返回成功之前会校验
最终结果、字节数、SHA-256 和正常 EOF。下载只有在完整校验后才替换本地目标。

## 命令行

```sh
loom-vm-files --observer "$LOOM_OBSERVER_URL" --token-file /private/grip-token \
  --activation "$ACTIVATION_URI" upload ./input.bin /home/worker/input.bin

loom-vm-files --observer "$LOOM_OBSERVER_URL" --token-file /private/grip-token \
  --activation "$ACTIVATION_URI" download /home/worker/result.bin ./result.bin
```

HTTP Observer 需要显式 `--allow-http`。子命令可加 `--overwrite` 和 `--timeout 600`。
调用方只提供业务路径；不要从 activation 提取 SSH 私钥或把凭据放进文件动作参数。

## 文件行为

- 远端路径必须为绝对路径，父目录须已存在；上传和下载仅处理普通文件，不跟随最终文件名处的符号链接。
- 默认拒绝覆盖；目标出现竞争创建时也会失败。显式覆盖采用同目录临时文件和原子替换，不会原地破坏旧文件。
- 上传提交前会验证声明大小和 SHA-256。提前断线、校验失败或不完整提交会清理临时文件，保留原目标。
- 上传和本地下载产生的文件权限为 `0600`，包括显式覆盖；不复制原文件的权限、属主或时间戳。
- 下载从一个打开的文件描述符读取；若发现源文件在传输期间改变则拒绝结果。客户端对数据逐块计算摘要。
- 流不是可重放任务，不支持断点续传。若发送完整上传提交后连接丢失，远端可能已经完成替换；调用方应通过下载比对大小和摘要确认结果，不自动重新覆盖。
- 进程或主机异常终止可能遗留以 `.loom-upload-` 开头的临时文件；普通连接关闭和超时会尝试关闭 SSH 输入并清理。

## `vm-file.v1` 字节协议

控制帧为 4 字节网络序无符号长度和 UTF-8 JSON，JSON 最多 65536 字节。文件主体为
原始字节，可以跨任意数量的传输帧；网络帧边界不代表文件边界。无需 half-close。

stream-open 回执仅为 `{"protocol":"vm-file.v1","direction":"upload"或"download"}`，
表示通道接纳；之后的结果帧才说明文件操作是否成功。

上传输入为 `{path, size, sha256, overwrite?, timeout_seconds?}`，sha256 为小写十六进制。

1. 服务端先检查目标并创建临时文件，返回 `{status:"ready", path, size, sha256}` 控制帧。
2. 客户端发送恰好 `size` 字节，再发送 `{type:"commit", size, sha256}` 控制帧。
3. 服务端核对实际大小、摘要和 commit，flush/fsync 后原子落盘，返回
   `{status:"uploaded", path, size, sha256}` 控制帧，然后正常 EOF。commit 是提交边界。

下载输入为 `{path, timeout_seconds?}`：先返回 `{status:"downloading", path, size}`，
随后恰好 `size` 字节，最后返回 `{status:"downloaded", path, size, sha256}` 和正常 EOF。
调用方必须验证尾帧及实际数据摘要后才使用文件。

操作失败尽可能返回 `{status:"error", error_code}` 控制帧；中途文件缩短、网络断开或
授权到期也可能直接导致不完整流。无有效成功尾帧和完整性校验，就不能报告传输成功。
