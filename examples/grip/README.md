# GRIP 客户端示例

先安装 `packages/loom-orchestration`，设置部署者提供的 `LOOM_OBSERVER_URL`、
`LOOM_GRIP_AUTHORITY`、`LOOM_WORKSPACE_ID`、`LOOM_GRIP_TOKEN`。以下命令在仓库根目录运行。
本地 HTTP 入口需显式加 `--allow-http`。完整协议说明见 [资源使用指南](../../docs/grip-resource-usage.md)。

描述工作空间并查看可用动作：

```sh
python examples/grip/invoke.py
```

创建 `list.json`，内容为 `{"mcue":"computing/slave"}`，查询执行资源：

```sh
python examples/grip/invoke.py --action list-resources --input list.json
```

将 mcue 改为 `computing/driver`、`capability/package`、`capability/activation`、`computing/vm`、`execution/plan` 或 `execution/run` 可查相应资源。
使用 `grip/mcue-type` 查询类型定义，再对返回的定义 URI 调用 read 取得属性 Schema。
服务包发布方法见 [服务包指南](../../capability_services/README.md)。
复制返回的 Slave URI 和包 URI，先创建 `check.json`：

```json
{"package":"<package URI>","instance_name":"example"}
```

```sh
python examples/grip/invoke.py --target "$SLAVE_URI" --action check-package --input check.json
```

只有返回 ready=true 后才继续；否则按 issues 选择合适的节点或由节点管理员完成接入。
能力包部署不通过 SSH 修改数据入口、TLS 或 Observer 配置。然后创建 `activate.json`：

```json
{"package":"<package URI>","instance_name":"example","instance_config":{},"credential_profile":"<operator 配置的 profile 名称>","idempotency_key":"example-activate-1"}
```

```sh
python examples/grip/invoke.py --target "$SLAVE_URI" --action activate-package --input activate.json
python examples/grip/invoke.py --target "$ACTIVATION_URI"
```

`credential_profile` 属于激活实例，与 `instance_config` 并列；不需要挂载凭据的实例省略该字段。
请先确认 Slave operator 已配置对应目录。不同实例可选择不同 profile；同一实例的重试和重启
保留原选择，切换 profile 使用新实例名。凭据文件不进入请求或包 manifest。

激活结果的 `resource` 是实例 URI。SSH 实例配置改为实际 `{host,port,user}`；若返回
`pending_auth`，调用实例 `auth-start`，输入文件内容为 `{"expected_revision":1}`（使用真实 revision）。
完成返回的认证指引后，重新描述实例。

从 offers 选择完整业务 action URI。对该 action DESCRIBE 可查看原始 descriptor 和输入 schema URI。
创建 `call.json`，内容为 `{"arguments":<业务输入>,"expected_revision":<真实 revision>,"idempotency_key":"example-call-1"}`：

```sh
python examples/grip/invoke.py --target "$ACTIVATION_URI" --action "$BUSINESS_ACTION_URI" --input call.json --wait 120
```

| 包 | descriptor 操作名 | 业务输入示例 |
|---|---|---|
| Kubernetes | resources-list | `{"resource":"pods"}` |
| Outline | documents-list | `{}` |
| Suanli Cloud | regions-list | `{}` |
| 普通 VM SSH | exec | `{"command":"hostname","timeout_seconds":30}` |
| Suanli VM SSH | exec | `{"command":"hostname","timeout_seconds":30}` |

此表的操作名用于识别 descriptor；调用时使用描述返回的完整 URI。每个输入都受该动作的 schema 约束。
`--wait` 有界轮询返回的 action；到达上限仍返回原任务 URI，可稍后读取。
一个普通、无需交互认证的包也可直接在 Slave 上调用 `run-package`，输入
`{package,action,arguments,idempotency_key}`，仍返回同一种 Run 资源。

完整原生计划示例使用 SDK 0.10 的 `publish_function`、`OrchestratorPackage` 和
`SubmissionBuilder`，发布倍增函数及编排包，再通过发现的 Driver 调用 `submit-plan`：

```sh
python examples/grip/native-task.py --request-id native-double-1 --value 21 --wait 120
```

本地 HTTP 加 `--allow-http`。示例需要支持 `process:json_stdio/1` 的 Slave；执行时
Driver 还需支持 `container:python_orchestrator/1`。Driver 离线仍可冻结计划并排队：
`accepting_submissions` 与 `available` 分别表示接收入口和执行租约状态。
输出保留提交、Run 与不可变 Plan URI，默认输入的完成结果应为 `{"value":42}`。
省略 `--wait` 立即返回提交视图；本地轮询超时不取消任务。

`--request-id` 是业务幂等键，SDK 将它发送为 `idempotency_key`；GRIP 消息 `request_id`
由 SDK 为每条消息生成。未知提交结果时复用同一命令或调用 `client.submission(key)`。
改变输入需新业务键。可对返回的 Run 调用 `read`（输入 `{"include_history":true}` 查看证据），
对 Plan 调用 `read` 查看完整冻结闭包。`client.run(run_uri)` 默认包括历史。
修复使用 Run 的 `resubmit` 与当前 `base_closure_version`、新键和完整替换计划；
attestation 的接受或待决任务的放弃使用 `resolve`，终态任务可 `close`。

停止实例的输入为 `{"expected_revision":<当前 revision>,"idempotency_key":"example-stop-1"}`，
以实例为 target 调用 `stop`。复用同一幂等 key 可查询同一操作结果；改变参数应使用新 key。
