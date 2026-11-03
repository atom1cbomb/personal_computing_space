# Pool 完整计划入队与增量状态写入

日期：2026-11-03，Asia/Shanghai。接续[状态查询与作用域优化](2026-11-02-driver-pool-scoped-queries.md)。

已实现并部署三项调整：完整计划提交跳过交互式规划状态；Run 写入明确限定字段；就绪检查按引用读取包。并发 32 的同规模客户端观测吞吐从 2.48 提升到 4.19 个任务/秒，完成 P95 从 16.89 秒降到 10.60 秒。

## 流程变化

| 路径 | 之前 | 现在 |
| --- | --- | --- |
| 完整计划提交 | `open_run → begin_refinement → commit → 记录提交`，在同一事务内多次保存 Run | 校验后直接创建 `committed` Run，一次保存 Run，再写执行队列；两者同事务提交或回滚 |
| 完整计划修复重提 | 进入 thinking、保存草稿，再调用 commit 重复检查与保存 | 保留修复权限、版本、成功条件和预算约束检查，直接冻结新版本并入队 |
| 领取到启动 | claim 检查就绪，start 再检查一次；创建 Attempt、设置所有者、记录领取分别保存 | 一次就绪检查，将执行状态、Attempt 与所有者信息合并保存 |
| 状态写入 | 每次构造完整 Run 持久化值，序列化 draft、committed、能力包、节点等 | 操作明确指定字段，只读取对应列、序列化相关对象、更新变化列并追加事件 |
| 就绪检查 | 刷新 Slave 目录时载入整个 workspace 的包与 activation | 读取 Slave 注册信息，按计划中的包引用定向查询；编排仅展开声明的节点包 |

冻结计划仍是必要职责：执行和恢复需要确定的版本与摘要，重复提交也必须绑定原计划。删除的是完整计划路径中的交互式中间状态和重复处理。需要逐步构建草稿的交互式路径仍使用 thinking。

提交时与领取时分别检查资源仍有意义，因为排队期间包可能被撤回、Slave 可能离线；本次移除的是同一次领取过程中的重复检查。Run 私有候选包在当前修改尚未保存时，使用本次 Run 的状态，避免被旧索引覆盖。

代码：[提交、就绪与持久化](../../../loom_v2/pool/tasks.py)、[资源作用域](../../../loom_v2/observer/resources.py)、[清理状态写入](../../../loom_v2/pool/cleanup.py)。操作说明见 [Driver Pool](../../driver-pool.md)。

## 验证与发布

新增 PostgreSQL 回归覆盖：

- 完整计划只插入一次 Run，无中间 UPDATE，无 thinking 事件；领取只检查一次就绪状态。
- 执行队列写入失败时，Run、能力索引、事件与队列一起回滚。
- 关闭 Run 不调用计划序列化，SQL 不读取或更新计划等大字段，并保留其他列的独立变更。
- 就绪检查的包 SQL 必须含具体 package_ref；无关坏包不影响当前任务，排队后撤回目标包仍会阻止执行。
- 新增的 Run 私有候选包在 patch 保存前即可参与正确的就绪判断。

主范围 `tests/api tests/driver tests/pool tests/db tests/slave tests/contracts` 得到 743 passed、17 skipped，另有 1 项编排测试在准备阶段返回 readiness_blocked。最终代码的串行相关回归 77 passed，覆盖该失败用例及新增测试；没有放宽生产租约或测试超时。首次运行时本机 Docker 未启动的失败、并行测试时的失败及复核结果均保留在日志中，不将主范围首轮描述为全绿。语法和 diff 空白检查通过。

10:19 完成生产 Observer、Pool 切换。切换前确认无活跃执行，停写并备份 PostgreSQL；保留原镜像和配置供回滚。数据库仍为 `011_run_capability_scope`，SDK 仍为 0.14.0。Driver 暂停后恢复原镜像；Slave 与业务能力服务镜像未变更。

- 发布标签：`pool-admission-20261103`。
- Observer、Pool 镜像：`sha256:2067afc97558de9f2042cc3b5ec9fec68050ed7e2b90bfa86fcc9a103b7b69f0`。
- 源文件清单 SHA-256：`b5e10a1b5e3bbb7e6eca9b4c3c79f48ccb51619facae4630fff4e514a046355d`。
- 相比上一生产发布，运行时代码仅改变 `pool/tasks.py`、`pool/cleanup.py`、`observer/resources.py`。

## 同规模复测

两个 Driver 各 4 个执行槽、三个 Slave、单节点纯 JSON 函数、相同独立安装的生产 SDK。客户端每 2 秒轮询；输入上传和计划构建不计时，先预热三个任务。未增加自动重试或放宽超时。下面两档运行时没有 CPU 采样器。

| 并发 | 完成数 | 上轮任务/秒 | 本轮任务/秒 | 上轮完成 P95 | 本轮完成 P95 | 上轮提交 P95 | 本轮提交 P95 |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 8 | 24/24 | 2.00 | 2.42 | 4.82 s | 3.95 s | 1.93 s | 1.22 s |
| 32 | 64/64 | 2.48 | 4.19 | 16.89 s | 10.60 s | 5.95 s | 3.34 s |

客户端观测吞吐分别提高约 21%、69%。追加的并发 32、128 个任务全部成功，吞吐 4.05 个/秒、完成 P95 9.04 秒。这是短时负载数据，不是长期容量承诺。

服务器结算时间戳采用另一口径：并发 8 吞吐从 2.25 到 3.07 个/秒；并发 32 从 2.49 到 4.40 个/秒。并发 32 的排队 P95 从 8.35 到 4.69 秒，领取至结算 P95 从 2.32 到 1.54 秒，提交开始至结算 P95 从 15.19 到 8.35 秒。

监控取得 85 次有效采样，无采样错误、无其他活跃业务 Run、无数据库锁等待样本，连接峰值为 20。PostgreSQL deadlocks 始终为 1，增量为 0。并发 32 的 5 次采样中，Pool CPU 平均为 81.64%；128 任务阶段的 11 次采样平均为 86.37%，执行密集区间仍可达到约 100%。

## 剩余热点与断连

在持续负载末段与附加诊断阶段使用 py-spy 非阻塞采样，取得 402 个有效栈，报告 155 次读取错误。样本用于定位线索，不据此给出精确 CPU 占比；主对比表不包含采样阶段。

采样显示，就绪检查之外的 `list_slaves → refresh_slaves` 路径仍会处理 workspace 包；JSON 解码、SQLAlchemy 对象创建、包缓存键规范化及复制也是可见开销。此次改动没有消除所有目录读取和数据处理成本。

附加诊断阶段计划 64 个任务，启动 48 个，客户端正常观察完成 47 个；另一次调用在 Pool 的只读 Run `INVOKE read` 发生 `RemoteProtocolError`，余下 16 个未启动。按已返回的 Run URI 核对，该任务已完成，ticket/value 正确且只有一个 Attempt。没有盲目重提。对应时段应用无重启、OOM 或错误日志；HTTP 断连根因仍未确认。

## 取消、清理与数据保持

5 秒延迟动作进入 running 后，通过公开 SDK 取消，约 0.52 秒观察到 cancelled；再等待 6 秒，状态仍为 cancelled。该检查证明取消结果未被迟到结果覆盖，不代表远端动作立即终止。

本次实际创建 268 个 Run：3 个预热、24 个并发 8、64 个并发 32、128 个持续负载、48 个诊断任务、1 个取消测试。全部 closed/settled，均只有一个 Attempt；267 个保留 completed 结果、1 个保留 cancelled 结果。生产事件查询确认这些 Run 中没有 thinking/refinement_started 事件。三个测试 activation 均已停止。

原有 1422 个 Run 业务字段摘要、119 个包摘要、11 个待决策 Run 保持不变。最终共有 1690 个 Run、120 个 workspace 包，活跃 Run 为 0，无等待数据库锁的连接，连接数回落至 10。两个 Driver 保持压测开始时的实例和 epoch（13、15）；Observer、Pool、双 Driver 健康检查均为 200，公开目录报告三个 Slave 与逻辑 Driver 可用。

本机发布与回滚证据：`production/releases/2026-11-03-pool-admission/`。
本机测试、计时、采样与清理证据：`production/benchmarks/2026-11-03-pool-admission/`。
远端对应目录：`/opt/loom-production/releases/pool-admission-20261103/`、`/opt/loom-production/benchmarks/pool-admission-20261103/`。
