# 仅凭 skill 的外部 Codex：修复与完整用例复测

本次修复了初轮的 GRIP 运行语义误判，并覆盖 T1～T7 的执行或阻塞检查。基础资源发现、普通远端计算、多节点汇总和既有服务调用已通过独立复核；取消后的完整收尾、认证等待恢复和新服务包部署仍不能作通过声明。各轮结果分别记录，不把不同 ZIP 的成功项拼成最终 ZIP 的一次全量通过。

遵循 [通用验证流程](skill-only-codex.md)，保留 [初轮失败报告](2026-10-27-skill-only-live.md)。基线提交为 `db04580`，本次改动包含在后续修复提交中。生产 Observer/Slave 未在本轮升级，未独立取得其部署提交号。

## 隔离、轮次与改动

测试操作员使用 `root@9.0.3.9:22`；被测 Codex 在 UID 1000 容器中，仅获得发布版项目 skill、SDK wheel、自然语言任务和必要连接认证。没有源码、其他启用 skill、资源主机 SSH 凭据、SSH 客户端或 Docker socket。每例使用空工作区、只读根文件系统、独立 bridge，并阻断 IPv4/IPv6 TCP 22 出站；实际 `skills/list` 与隔离预检均保存。模型没有收到操作员的修复指导或任务实现；修复后的测试另开全新会话。

固定 Codex CLI `0.164.0`、模型 `gpt-5.6-terra`，测试镜像为：

```text
sha256:9f4ae20c9dbbc5913c16343fc316766ab50667130b805e03d3de2b9d09801c77
```

公开入口为可信测试网络 `http://9.0.16.66:8080`，GRIP authority 为 `loom.local`，workspace 为 `workspace-default`。以下资源 URI 均位于 `grip://loom.local/workspaces/workspace-default/` 下。模型服务配置只提取授权连接字段，不继承宿主机历史或插件。

| 冻结轮次 | SDK | skill ZIP SHA256 | 本轮改动 |
| --- | --- | --- | --- |
| R2 | 0.8.0 | `c17fe96ad477531833c9ea0b08ed3cc9551cf1acefa3d68eadbcf9b7a4d33b57` | 区分协议 mode 与业务 interaction；补完整函数包发布契约 |
| R3 | 0.8.1 | `c2052c8b61baea5d411915bfdb14ca0edbde10689c72416c39cf6bf7c0a5560a` | 只读 `activation_ref()`、已有命名实例与编排引用指南 |
| R4 | 0.8.1 | `2c5f683e271e2e7ec949c09cfb147cab78e89d891f1619ed6f4c4472ce1f19fb` | SDK 编排协议类型对齐运行时 JSON mapping |
| R5 | 0.8.1 | `c9340a547f93d8fc2ec95f856d234b1559aee9ec57cd1c4336c9c45fb2d1252c` | 补充多输入节点的包装规则与汇总契约 |
| R6 | 0.8.1 | `e9cbc1424873085c3d2dfa331c7db202019d45b28ef411f40ea2c12b68fe062b` | 区分 GRIP 幂等键与提交 request_id，以及 Run URI 与 REST Run ID |

R2-T5-001、R3-T3-001、R3-T6-001 在 Docker 隔离网络创建阶段超时，未运行模型，记为 `blocked_environment`。保留准备失败记录后，使用新目录重试；没有放宽隔离。后续改为顺序启动隔离准备。

## 认证与本机、测试机的关系

统一 `loom-auth` 助手和浏览器运行在本机，不需要从 9.0.3.9 下载认证程序，也没有把服务 Cookie 复制给被测 Codex。认证属于远端激活实例；9.0.3.9 上的客户端通过公开接口使用同一已认证实例即可。

最初选中的 9 个实例中，3 个 Grafana、AutoPaper、算力 VM 共 5 个处于 pending_auth，4 个 Kubernetes 处于 degraded/auth_failed。登录后独立 DESCRIBE 确认这 9 个实例均为 `ready/authenticated`，revision 保持 1。历史 stopped 实例不等于需要重新登录。AutoPaper 助手会话显示过期时，其资源实际已 ready，后续真实业务调用也成功；不能把助手会话状态当作资源当前状态。

人工介入限于浏览器登录。没有撤销正常登录制造故障，没有通过 SSH 修复资源主机。下面的调用覆盖代表性实例，不表示所有 9 个实例的全部动作都已测试。

## 用例结果与独立证据

| 用例 | 执行轮次 | 独立结论 |
| --- | --- | --- |
| T1 资源与语义发现 | R2-T1-001 | passed：正确区分 GRIP request-reply 与 task，未创建业务 Run |
| T2 普通远端计算 | R2-T2-001 | passed：远端执行与五项数值、内容摘要一致 |
| T3 两 Slave 与汇总 | R2、R4、R5 | R5 passed；保留 R2 类型检查失败、R4 汇总契约失败 |
| T4 既有服务 | R2-T4-001、R4-T4b-001 | passed：3 个 Volcano 集群，以及 Kubernetes、Grafana、VM 调用 |
| T5 幂等与取消 | R4-T5-001、R6-T5-001 | R6 幂等和 Run 取消通过；完整收尾验收未通过，attempt 仍 created |
| T6 等待认证后恢复 | R2-T6-001、R3-T6-002 | 未通过等待恢复验收；修复后已认证实例调用通过 |
| T7 服务包部署 | R2-T7-001 | blocked_capability：已部署 Slave 无 check-package offer |

### T1、T2：初轮问题修复

R2-T1 能正确解释 Kubernetes、Volcano 的 `mode=request-reply` 表示传输交互，其 `interaction=task` 和 `run-output` 要求查询 Run。操作员独立 DESCRIBE 得到一致契约。

R2-T2 自行发布普通函数包，Run `run-2633f01d040c` 在 `slave-a` 的 attempt `attempt-638fe3690bea` 完成。独立按 Observer 返回引用读取结果：

```json
{"count":10000,"sum":50005000,"squares_sum":333383335000,"min":1,"max":10000}
```

规范化 JSON 的 SHA256 为 `e07f1750026dd8e14a9fd36daad9fb74557320959187da9f385ee8b86c2d1578`，与内容引用匹配。执行器描述 URI 在旧部署仍存在可读性缺口；此次依靠 skill 提供的版本化公开函数契约完成发布和执行，没有用内部接口绕过。

### T3：保留两次实际失败

R2 编排源码把外部 SDK 的 Pydantic ResourceRef 用在沙箱 mapping 接口中，提交被 `orchestration_program_type_error` 阻止，没有创建 Run。不是缺少两个可用节点。

R4 Run `run-146eb0e85336` 的两个分段分别在 `slave-a`、`slave-autopaper` 完成，但汇总错误地复用了分段函数及其输入 Schema，导致 `node_input_schema_mismatch`。agent 把两个 required-property 错误解释为“两段节点失败”；独立 attempt 记录证实失败发生在汇总输入，原报告未改写。补充规则为：单一 input ref 传原 JSON；多个 ref 组成 `{"inputs":[...]}`，汇总函数必须声明并处理该形状。

R5 使用相同自然语言目标、全新包前缀和隔离会话复测，最多 1 Run、5 节点、2 并发，执行期限 600 秒。Run `run-b1642ab0fc1b` completed，实际仅使用 3 节点：

| 节点 | 实际 Slave | attempt |
| --- | --- | --- |
| segment-1，1～5000 | slave-a | `attempt-41ef7dff1d08` |
| segment-2，5001～10000 | slave-autopaper | `attempt-3ca9ea202d2b` |
| merge | slave-a | `attempt-88df9680c986` |

独立获取三个节点的输入、结果和引用，确认 merge 的两个输入正是分段结果引用，最终 outcome 为 merge 结果。所有节点均 completed，内容摘要逐一匹配，五项整体统计与 T2 一致。该轮满足两台不同 Slave 的实际执行及同一 Run 内汇总要求。

### T4：真实服务结果

| 调用 | Run | 独立核验结果 |
| --- | --- | --- |
| Volcano 北京队列 | `run-a3e6ab93066a` | default、root 均 Open，status=ok、complete=true |
| Volcano 北京 ICT 队列 | `run-a25a74f3884d` | 同上 |
| Volcano 南京队列 | `run-f052128c0a30` | 同上 |
| Kubernetes 北京 Pod 列表 | `run-515b1c53761c` | status=ok，items=[]；明确为空列表 |
| Grafana 北京面板描述 | `run-6ae4e3b89245` | 取得真实 GPU Arena 面板和查询契约 |
| Grafana GPU 利用率 | `run-1b9ba3155e08` | 最近 30 分钟、60 秒步长，31 个真实样本；均为 0，complete=true、无 warnings |
| VM 简单算术 | `run-7181315792e0` | stdout=2+3=5，exit_code=0，未超时 |

所有调用均 completed，实际 attempt 在 `slave-a`；操作员独立取回内容，核对 RFC 8785 SHA256、报告值与执行时间。Grafana 的 0 来自样本值，没有把缺失采样解释为零。VM 使用已有 `vm-ssh-http/v5/slave-a/vm-9-0-16-113`，不是直接从测试机 SSH，也不是对新登录算力 VM 实例的额外业务验收。

T4b 的固定日期幂等键不适合作为以后轮次的唯一键；本次事件时间均落在会话区间，已确认不是复用旧 Run。7 个 Run 及其内容保留为证据；agent 的“未创建对象”只适用于业务资源。

### T5：幂等与取消

R4 同一 GRIP 幂等提交两次得到 `run-62a163022b3e`，只有一个 attempt，在 `slave-autopaper` 自然完成了 90 秒等待，终态 completed，取消验收失败。agent 先把完整 Run URI 传给 REST `run()`，修正为 Run ID 后，又把 GRIP `idempotency_key` 作为取消的 `request_id`，导致 404。独立查询确认该 request_id 没有对应提交；不能把这次错误解释成运行与取消的竞态。没有残留运行中的 attempt。

skill 已补充：GRIP 返回的 Run 可通过其公开 cancel offer 取消，或 `client.cancel(run_id)` 且省略 request_id；GRIP 幂等键不是 `client.submit` 的 request_id。R6 在全新隔离会话中，两次相同调用均返回 `run-1d8c7aa70306`。Observer 记录 running、execution_started、execution_claimed，随后进入 cancelled/interrupted，没有生成第二个 Run。

agent 自报 passed，但独立复核仅确认幂等与 Run 取消。其 `attempt-40b6b1c2db19` 指向 `slave-a`，多次读取仍为 created；没有远端进程已开始及已终止的完整证据。Slave available=true 只能证明节点在线，不能替代该 attempt 的终态或无残留进程证明。因此 T5 的完整收尾验收不记 passed；也不能反向断言远端进程一定仍在运行。当前公共状态的收尾证据缺口保留待查，不通过主机 SSH 越界补证。

### T6：已认证调用不等于等待恢复

R2 Run `run-3315010c00c2` 使用 Slave `run-package`，绑定了 Kubernetes 的 `default` 实例，而非报告所称的已有 `nanjing` 实例，终态为 `capability_activation_not_ready`，从未 awaiting_auth。它还创建了失败的 default 实例，不能接受 agent 的“没有创建实例”结论。操作员用创建幂等键核对本次 execution 所有权，经公开 stop 停用该实例，确认 stopped、revision=2；未改动既有命名实例。

修复后的只读 `client.activation_ref()` 保留 Observer 返回的包 digest、Slave、实例名、revision，不通过 ensure_activation 获取引用，以免更改现有配置。R3 使用已有 AutoPaper 实例，Run `run-62232e1fba63`、attempt `attempt-0dedfe6b0e38` 在 `slave-a` 完成，返回 37 个 venue 目录项；结果摘要 `ebd0564cb57cd35034b1962edcf266ef8ecbb93e225c339fd2ae6d7d1a138c33` 独立匹配。

但 AutoPaper 提交时已认证，没有 `awaiting_auth` 事件。该结果只证明已有命名实例可用于持久 Run，不能替代 T6 的等待、用户登录、同一 Run 恢复证据。本次没有再撤销登录或创建临时实例。真实认证等待恢复仍需专门的未认证测试实例和协调后的独立验收。

### T7：当前部署的公开能力缺口

agent 已发布 `packages/skillval-20261027-root-001-service/1.0.0`，digest 为 `002e83aa16c76689de112564d23059b40d912358ae2d9ab9f11c3c9c3b2409c6`，使用公开发现的既有不可变 embedding 镜像。操作员确认包存在且没有任何激活实例。

两个 Slave 的公开 offers 只有 `run-package/v1` 和 `activate-package/v1`，没有必需的 `check-package`。因此停止在发布后，未激活或调用新服务，也没有绕过检查通过 SSH 部署。该限制需要升级已部署的公共协议支持，重新登录不能修复。发布记录和内容有意保留。

## 本地回归、证据与清理

激活项目虚拟环境后，完整 pytest 为 **937 passed、35 skipped**。后续编排类型和文档修改对应的集中检查为 **51 passed**，覆盖公开发布运行、语义契约、命名实例引用、编排类型预检及隔离运行器。第一次未激活虚拟环境的全量执行有 42 个环境失败（Pyright PATH、子进程 Python 依赖）；修正环境后全部消失，原日志保留。跳过项不计作通过，也不替代真实外部验收。

完整提示词、摘要、预检、JSONL、stderr、agent 代码和原报告位于测试机 `/root/loom-skill-validation/evidence/`，开发机归档于 Git 忽略的 `production/skill-validation/20261027-r2/` 至 `20261027-r6/`。独立 Run/内容快照与 `*-independent-review.json` 单独保存，不覆盖 agent 自报。

12 个业务会话及 3 个准备失败目录共扫描 240 个证据文件（不包含安装依赖），真实 GRIP token 和模型 API key 的精确匹配均为零。命令与生成代码检查未发现资源主机 SSH、内部控制接口调用或项目源码下载；这不等于完整网络审计。Gitleaks 8.30.1 在原始证据中产生 23 个命中，逐项核对均为公开幂等键，不是认证凭据。待提交的 16 个文件另经 Gitleaks 和已知凭据精确扫描，未发现密钥。

本轮测试容器及独立网络均已删除。本机认证助手和专用浏览器已关闭，临时浏览器 profile 已删除；结束前再次 DESCRIBE 确认 9 个目标实例仍 ready/authenticated。测试机本轮复制的两份认证文件在最终扫描后删除，原有账户与资源认证保留。

原始日志、业务数据、认证文件、浏览器 profile 和发布 ZIP 均不进入 Git。函数包、编排包、Run、运行期函数激活记录和不可变内容作为验证证据保留；已存在业务实例不属于本轮清理范围。取消 Run 的 attempt 状态缺口与测试容器清理是两个不同结论。
