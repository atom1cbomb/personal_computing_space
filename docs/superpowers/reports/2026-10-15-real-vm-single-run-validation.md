# 真实 VM 单 Run 与浏览器认证验收

**日期：** 2026-10-15\
**状态：** 进行中，尚未宣称真实浏览器认证与 SSH 使用通过。\
**环境：** `production/deployment.toml`，Observer `http://9.0.16.66:8080`。\
**外部 agent：** `9.0.16.66:/root/pcs_test/metadata-20261015`，仅使用 skill、独立 SDK 和公共 API。

## 实测发现与修正

1. 沙箱 `emit_node` 原实现不接受公共 SDK 声明的关键字调用。已对齐位置、关键字和混合调用，并补充协议测试。
2. 原 readiness metadata 使用 `instances-status`、`RUNNING` 和 `Ip`，但真实状态接口只返回身份与小写状态，不返回 IP。改用 `instances-list` 的 `result.Instances[]`，严格匹配身份、`State=running` 与 IP；同步实际列表 schema 和 spec/skill，不通过删除断言绕过。
3. 动态访问节点原来丢失具名 activation，按 default 实例下发，且 provision 未携带目标资源与连接配置。现在保留绑定、配置及实例名，核对 package、Slave、实例与 revision；本地生命周期测试检查实际 Worker 侧 activation。
4. 云网关签名截断了小数秒，同秒重复查询会产生相同签名，触发 provider 的 replay protection。保留 RFC3339 小数秒，并验证同秒不同时间的签名不同；不绕过签名或认证校验。
5. SSH IoContract 原来只有 success_semantics。新增真实 validator 检查 `exit_code == 0` 且 `timed_out == false`，保留原成功语义，不自动接受 attestation。
6. skill 明确 `resource_budget.execution_timeout_seconds` 和 `allowed_effects` 必须取精确 export.permissions，避免外部 agent 猜字段或权限名。
7. `wait_activation` 的实际沙箱 RPC 未将 activation_ref 从 JSON 还原为 ResourceRef，真实 VM readiness 通过后因此失败。补齐反序列化，并通过真实 bootstrap 子进程的 ensure→wait 协议测试。
8. workspace activation 写入与 discovery 刷新缓存存在竞态，导致绑定丢失。写入保存显式快照，按 package 串行化同一持久化行的 lifecycle 更新；SQLite 并发 discovery/两个实例及重启读取回归通过。
9. `ensure_activation` 原来只记录 desired state，没有将 provision 命令下发给 Worker。现在使用已落库的精确实例、revision、资源与配置下发，记录实际健康报告；已就绪或等待认证的实例不重复 provision。生命周期测试不再手动伪造首次 pending_auth，而要求实际 provision 回报触发等待。
10. R3 在等待 activation 时发生 `observer_unavailable`，旧日志未保留底层传输异常类型，具体网络原因尚未证实。新增不含秘密的异常类型诊断；只读查询遇到传输失败最多尝试三次，`node.execute`、activation 创建等写请求不自动重放。注入断连/超时测试验证边界。

## 部署与回归

- Observer 与 Driver 已部署绑定/协议修正；云网关通过独立部署 wheel 构建镜像，没有向外部测试目录复制服务端源码。
- 新云镜像：`localhost:5000/loom/suanlicloud-gateway@sha256:ec182db180869314165e134f1816b4936020e5696decbb5865adc654f217a9fb`。
- 新包：`suanlicloud-http/metadata-e2e2-20261015`、`suanli-vm-ssh-http/metadata-e2e2-20261015`；保留旧版本，不覆盖不可变身份。
- 新 skill：测试目录内 `personal-computing-space-real.zip` / `skills-real/personal-computing-space/`。
- R4 部署前完整回归：**473 passed，30 skipped**。contract 生成一致性、skill 校验与 diff 空白检查通过。
- R4 Observer/Driver 部署 wheel SHA256：`0c8d499d7ede224da08b99da3e13d9480ce777aeb4629203e8d18743a95f6dda`；Driver epoch `8918`。

## 初轮真实执行证据

| Run | VM | 结果 |
| --- | --- | --- |
| `run-8a5c141cbc05` | `6136232c-574e-4480-98c1-35a913b4b606` | create 成功；状态查询后 activation 不可用；cleanup completed |
| `run-a9b06883d575` | `5bd08d97-27d9-4fe8-acd8-b9c19baa549b` | create 成功；多次状态查询后 activation 不可用；cleanup completed |

上述初轮终止操作仅返回 `terminating`；随后只读 Run `run-6c7a9cde6346` 已确认这两台及 R2 VM 均为 `terminated`。初轮不计为认证或 SSH 成功，也没有自动 resolve 其 awaiting_decision。

复测日志为 `agent-events-real-vm-r2.jsonl`，进度文件为 `real-vm-progress-r2.json`；浏览器认证由用户本人完成，认证秘密不进入源码、内容存储或验收记录。

## 后续真实执行证据

| 轮次 | Run / VM | 结果 |
| --- | --- | --- |
| R2 | `run-62e5444b9e25` / `c6d951ea-5de5-474b-a69b-61fb05958b75` | create、精确身份 readiness 与 IP 成功；RPC 反序列化失败；自动 cleanup completed，独立查询 terminated |
| R3 | `run-fd919289e7d8` / `ba332096-f637-4fa3-b90e-b6f2d870b72e` | create、readiness、记录具名 activation 成功；尚未实际部署访问实例，等待期间 observer_unavailable；自动 cleanup completed，独立只读 Run `run-9812cfb537f9` 确认 terminated |

以上均未进入浏览器认证，不算端到端通过。R3 的失败不是用户认证失败，也不能据此认定网络断连的具体原因。

## R4：真实访问实例已进入认证等待

- 外部 agent 提交：`extreal-20261015-r2-r4-20261015-auth-e2e-e1`。
- Run：`run-53e9aadac5d6`；execution：`execution-6c354344d736`。
- VM：`a9490a1b-3744-4cea-bd15-98103be31bf7`；仅一次 create，九次只读 readiness 查询后满足条件（`State=running`、`Ip=9.0.16.108`、`AgentOK=true`）。外部 agent 的初始摘要误取较早的空 IP 结果；以 Run 最后匹配身份的 readiness 证据为准。
- 实际部署的 SSH activation：package `suanli-vm-ssh-http/metadata-e2e2-20261015`，Slave `slave-a`，instance `loom-real-e2e-20261015-r2-r4-20261015-auth`，revision `1`；绑定该 VM 的 ManagedResource，可通过公共 resource activations API 倒查。
- Run 已真实进入 `awaiting_auth`，waiting 指向上述精确 activation。未调用 auth/start，等待用户准备浏览器侧 localhost 隧道后才生成短期挑战。
- 尚未验证登录、同 execution 恢复、SSH 输出与本轮最终清理；不能将此状态写为完整通过。
- 本轮主执行截止为北京时间 `2026-10-15 18:58:04`，清理预算另为 300 秒；用户未及时登录时不可宣称验收完成，应先核对本轮终态和清理再决定下一轮。
- 用户准备好浏览器时已过截止；复查 R4 为 `failed / execution_deadline_exceeded`，同 execution 的 cleanup completed。终止回执 `req-09d70b5ae0b6d9a0` 返回 `running → terminating`。本轮没有启动登录挑战，不计为登录失败或完整验收通过；R5 必须先独立核验终止状态。

## R5：用户准备好后的浏览器验证

- 外部 agent 先通过只读 Run `run-8fa4ce9c418b` 确认 R4 及之前测试 VM 全部 `terminated`，然后才进行一次新建。
- Run：`run-0de77cb48c69`；execution：`execution-37b03a3019fb`。
- VM：`a9e281c7-ed2f-4164-8a2a-efc1005a5c94`；实例名 `loom-real-e2e-20261015-r2-r5-20261015-auth`。
- 已进入 `awaiting_auth`，用户确认浏览器准备好后，按精确 activation 调用一次 auth/start 并提供五分钟登录链接。桥接令牌仅驻留 helper 内存，未写入编排源码或验收记录。
- 待核验：用户完成登录、同 execution 自动恢复、四条只读 SSH 命令、唯一 create 与最终清理。
