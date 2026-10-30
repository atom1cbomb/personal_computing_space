# VM 文件流实现与验证

新增普通 VM `vm-ssh-http/v6`、云 VM `suanli-vm-ssh-http/v7` 的
`files.upload` / `files.download` byte-stream 动作，以及 SDK 0.12.0 的
`VmFileClient` 和 `loom-vm-files` CLI。两个动作复用所属 activation 的 SSH
目标、凭据、host-key 校验和云认证续期。命令执行仍使用原 task 契约。

共享 ASGI 网关取代两份旧 HTTP Handler；现有 exec/auth hooks 保留，文件操作走
GRIP WebSocket。目标端通过同一 SSH 连接运行临时 Python 3 stdlib 程序，无需常驻
文件服务。传输按 64 KiB 分块，使用显式长度和提交帧，不依赖 half-close。

上传先准备同目录临时文件，再接收数据与 commit，核对大小和 SHA-256 后原子创建
或替换；下载校验成功后才发布本地文件。默认拒绝覆盖，不跟随最终路径处的符号
链接，支持空文件和二进制内容。目录须已存在，输出文件权限为 0600。提交后丢失
响应属于结果不确定，不自动重放。完整协议见 [使用说明](../../vm-file-transfers.md)。

## 验证

最终回归命令：

```sh
.venv/bin/pytest -q tests/capability_services \
  tests/contracts/test_grip_resource_names.py \
  tests/contracts/test_orchestration_client.py \
  tests/contracts/test_server_wheel.py \
  -m 'not integration and not e2e' --disable-warnings --tb=short
```

结果为 **414 passed、2 skipped，113.22 秒**。本机额外准备 `/run/sshd`，使真实
SSH 测试全部执行；这些用例在没有 root/sshd 环境中明确跳过，不用模拟结果代替。

- 两个网关分别通过真实 loopback OpenSSH、临时密钥和 WebSocket 完成
  **21 MiB + 3 字节**二进制文件往返，验证大小、摘要、完整字节及权限。
- 覆盖空文件、带引号/Unicode/shell 符号的路径、已有文件拒绝和显式覆盖、
  错误摘要、超长输入、提前断线清理、超时、符号链接、缺失源文件、源文件改变、
  下载截断/损坏保留本地原文件、两个并发创建中仅一个成功。
- 完整公开链路为 SDK → Observer GRIP 授权与转发 → 网关 WebSocket → 实际 SSH。
  两类网关均完成上传/下载，activation revision 和认证状态保持不变，不产生 Run。
  无效令牌及停止后的旧访问均被拒绝。
- 既有命令执行、浏览器认证、续期和服务包契约回归通过。命名空间测试改为读取
  云创建描述符的实际固定依赖，不再硬编码已过时的包版本。
- 契约生成器 `--check`、`git diff --check` 通过；新模块、SDK 和普通 VM 网关的
  Pyright 检查为 0 errors。
- 两个 Dockerfile 构建成功，容器以原非 root 用户启动，`/healthz` 和文件流路由
  检查通过；测试容器已删除。
- 独立 SDK wheel 和 skill ZIP 构建通过；在仓库外从独立 wheel 导入客户端及运行
  `loom-vm-files --help` 成功。

首轮大文件测试发现目标已存在时客户端可能尚在发送正文就遇到连接关闭，现已通过
上传 `ready` 帧解决：远端检查与临时文件准备成功后，客户端才发送文件正文。

## 发布状态

本次完成源码、包模板、SDK、镜像构建和本地完整链路验证，**尚未发布或切换生产
activation**。旧版本包不可变，不会自动获得新动作。云 SSH 新摘要使用独立状态卷，
首次使用仍需为新 activation 登录；文件动作自身不会创建另一套认证或复制凭据。
现有云创建包继续固定引用 SSH v6，本次未改写其创建契约。

私有构建和测试日志保存在 `/tmp/loom-vm-stream-*`；独立 SDK/skill 产物位于
`/tmp/loom-vm-stream-sdk/`。本地构建标签为 `loom-vm-ssh:file-stream-test` 和
`loom-suanli-vm-ssh:file-stream-test`，没有推送生产镜像仓库。
