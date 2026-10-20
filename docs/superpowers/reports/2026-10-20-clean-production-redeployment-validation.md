# 9.0.16.66 全新部署验收

日期：2026-10-20。设计：[Driver 配置清理与生产重新初始化](../specs/2026-10-20-clean-production-redeployment-design.md)。

## 实现

- Driver 的部署配置只保留 `workspace_path`；删除 Codex/Volcengine 配置、模型密钥读取与上传、model catalog 上传、启动时生成 Codex 配置的逻辑。旧模型字段被拒绝。
- 同步公共 deployment 示例、`.env.example`、README 和本地忽略的生产 inventory。
- 修复 Observer 部署归档遗漏 `grip_token` 的问题，保证远端 secret 挂载存在，并在错误诊断中脱敏。
- 生产 GRIP authority 为 `loom.local`，Observer 地址为 `http://9.0.16.66:8080`。新 token 保存在本地忽略文件 `production/secrets/grip_token`，不写入版本库。

## 远端清理与部署

按 Compose 项目标签和 runtime 的 workspace/slave 标签确认归属后，删除原有 6 个核心服务容器、19 个能力容器、20 个持久卷和 5 个项目网络。持久卷包括两套 PostgreSQL、MinIO、runtime sockets、能力实例状态、Driver 的旧 Codex 状态以及遗留 Loom 数据卷。

删除旧角色部署目录、旧 release 目录、Driver 工作区和该 workspace 的 runtime 状态；保留 `/var/lib/loom/runtime-credentials` 中的 Kubernetes、Outline、算力云和 SSH 凭据，以及已有镜像仓库。未向外部云服务发送资源删除请求。

重新上传源码、构建应用镜像，依次启动 MinIO、Observer、Slave 和 Driver。Observer 和 Slave 均从空 PostgreSQL 数据卷迁移到 `005_slave_action_name`；Slave 执行账本只有 `action` 列，没有旧 `operation` 列。

实际运行的 Driver 无 Codex/OpenAI/ARK/Volcengine 环境变量、模型密钥挂载或 Codex 状态卷。

## 验证

本地相关测试：

```text
PATH=/root/loom_v2/.venv/bin:$PATH .venv/bin/pytest -q \
  tests/deployment tests/driver/test_control_client.py tests/driver/test_service.py \
  --disable-warnings
67 passed, 199 warnings in 4.86s
```

线上使用 SDK、真实 Observer/Driver/Slave、PostgreSQL 和 MinIO，发布一个 `process:json_stdio/1` 函数包执行验收：

| 检查 | 结果 |
| --- | --- |
| 四个服务健康检查 | 全部通过 |
| 未携带认证的 GRIP 请求 | `authentication_required` |
| Slave 发现与 DESCRIBE | 返回 `grip://loom.local/workspaces/workspace-default/slaves/slave-a`，提供 `run-package` 和 `activate-package` |
| 内容上传 | 返回带 `deployment-smoke/schemas/value` 可读名称的 GRIP URI |
| 包发布 | Observer 返回 `grip://loom.local/workspaces/workspace-default/packages/deployment-smoke-double/v1`，与目录查询一致 |
| 激活与 DESCRIBE | 返回激活资源 URI，状态 `ready`；描述包含声明的业务 action 和可调用 offers |
| 调用激活资源 | 输入 `21`，Run 为 `completed`，结果 `42` |
| Slave `run-package` | 输入 `2`，Run 为 `completed`，结果 `4`；相同幂等键返回同一 Run |
| 停止激活资源 | 状态 `stopped` |

本次线上验收覆盖函数执行链路；各业务 HTTP 能力包的验证范围见[能力包 action 验证记录](2026-10-20-grip-actions-and-activation-description-validation.md)。

## 最终状态

完成验收后再次删除并重建数据库、内容存储与运行时持久卷，清除验收数据；复用本次新构建的应用镜像重新启动。

- Observer 的 Run、执行队列、能力包、领域资源、业务幂等记录、会话和消息表均为空。
- Slave 执行记录为空；能力实例和能力容器数量为零。
- `loom-content` bucket 对象数量为零。
- Driver `driver-default` 与 Slave `slave-a` 均有有效注册，四个服务健康检查通过。
- 数据库中的运行注册及 `execution.claim_next`、`execution.recover`、`run.recovery.list` 回执是本次启动和空队列轮询产生的系统记录。

当前业务能力包需要重新发布和激活，服务凭据可继续使用。
