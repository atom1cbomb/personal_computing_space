# GRIP 客户端示例

先安装 `packages/loom-orchestration`，设置部署者提供的 `LOOM_OBSERVER_URL`、
`LOOM_GRIP_AUTHORITY`、`LOOM_WORKSPACE_ID`、`LOOM_GRIP_TOKEN`。以下命令在仓库根目录运行。
本地 HTTP 入口需显式加 `--allow-http`。完整协议说明见 [资源使用指南](../../docs/grip-resource-usage.md)。

描述工作空间并查看可用动作：

```sh
python examples/grip/invoke.py
```

创建 `list.json`，内容为 `{"kind":"slave"}`，查询执行资源：

```sh
python examples/grip/invoke.py --action list-resources --input list.json
```

将 kind 改为 `capability-package`、`activation`、`resource` 或 `run` 可查相应资源。
服务包发布方法见 [服务包指南](../../capability_services/README.md)。
复制返回的 Slave URI 和包 URI，创建 `activate.json`：

```json
{"package":"<package URI>","instance_name":"example","instance_config":{},"idempotency_key":"example-activate-1"}
```

```sh
python examples/grip/invoke.py --target "$SLAVE_URI" --action activate-package --input activate.json
python examples/grip/invoke.py --target "$ACTIVATION_URI"
```

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

停止实例的输入为 `{"expected_revision":<当前 revision>,"idempotency_key":"example-stop-1"}`，
以实例为 target 调用 `stop`。复用同一幂等 key 可查询同一操作结果；改变参数应使用新 key。
