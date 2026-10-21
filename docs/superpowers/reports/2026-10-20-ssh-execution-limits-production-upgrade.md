# SSH 执行限制生产升级验收

2026-10-20，在 `9.0.16.66` 完成升级。设计见 [SSH 能力包执行限制](../specs/2026-10-20-ssh-execution-limits-design.md)。

## 部署与保留数据

发布目录：`/opt/loom-production/releases/ssh-limits-20261020-085334`。
基础提交为 `117a13f`，实际部署包含本次未提交修改的源码快照，SHA-256 为
`406d1810f8128b70097c39376a69651a36d8ce6f503a6b23549483c45e75b2d4`。

升级前备份两个 PostgreSQL 数据库、角色配置、SSH 持久状态、旧源码及三个核心镜像。
备份保存在发布目录的 `backup/` 下。依次重建并启动 Observer、slave-a、Driver，保留原数据库、
MinIO 内容和注册资源。原有 6 个 Run 均仍可查询；升级预检及验收新增 4 个 Run，最终共 10 个。

生产配置已核实：Slave action 预算为 3660 秒，Worker 转发等待为 3720 秒，
runtime/plugin 业务响应上限为 256 MiB。三个核心服务健康，Driver epoch 为 2，
Observer 报告 `execution=true`、`orchestration=true`。

## 包与激活实例

所有下列资源共享前缀 `grip://loom.local/workspaces/workspace-default/`。

| 包 | 发布结果 | 当前激活 |
|---|---|---|
| `packages/suanli-vm-ssh-http/v3` | published | `activations/suanli-vm-ssh-http/v3/slave-a/loom-v2-auth-3c474f5bb5`，ready |
| `packages/vm-ssh-http/v3` | published | 当前无实例 |
| `packages/suanlicloud-http/v2` | published，VM 创建契约依赖 SSH v3 | `activations/suanlicloud-http/v2/slave-a/default`，ready |

SSH 镜像使用远端仓库返回的不可变名称：

- `localhost:5000/loom/vm-ssh-gateway@sha256:24a67291ff99a86f989e8c8294148e18f13bb3cf3b8666ed5f4564ed1a875e2e`
- `localhost:5000/loom/suanli-vm-ssh-gateway@sha256:e438c9977a0d167b2a58c903ff0fa454cca3bea0dacd9aefa645baa1b0df2de8`

旧 SSH v2 和算力云 v1 激活实例均已通过 GRIP `stop` 停止；旧不可变包保留以支持历史 Run 的溯源。
同一 VM 的 SSH 认证状态迁移到 v3 的持久卷，并通过实际连接验证，无需重新登录。

VM 仍为 `resources/vm/loom-v2-auth-3c474f5bb5`。其 DESCRIBE 的 `activations` 与 `offers`
已唯一指向 SSH v3；`metadata.package` 保留最初注册时的 v2 引用，表示原注册来源。

## 线上验收

验收通过 Observer 的公开 GRIP 接口完成，实际命令在原 VM 上执行。

| 验收 | 结果 | Run |
|---|---|---|
| 新激活实例执行 `id` | root，退出码 0 | `run-046a0f0d0444` |
| 205,053 字符脚本，省略 timeout_seconds，运行 130 秒，stdout/stderr 各写入 5 MiB | 实测 130.0795 秒；退出码 0，未超时、未截断；stdout 5,242,955 字节（含计时信息），stderr 5,242,880 字节 | `run-63fe3ac5dc1b` |
| 新版算力云查询原 InstanceId | 原 VM 名称一致、状态 running | `run-fe48735d1a6b` |

输入 Schema 已读取核对：命令最多 1,048,576 字符，timeout_seconds 为 1–3600 秒。
当前目录共有 174 项：1 workspace、1 slave、86 action、8 package、4 activation、1 VM、10 Run、63 schema。
两个新版激活实例为 ready，两个旧版激活实例为 stopped。

本地详细记录位于忽略目录 `production/ssh-upgrade-{state,packages-20261020,validation-20261020}.json`，
当前目录快照为 `production/resources-latest.json`；同份验收记录和源码快照保存在远端发布目录。
