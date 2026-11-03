# Driver Pool 状态查询与 Run 能力作用域优化

日期：2026-11-02，Asia/Shanghai。接续[上一轮性能修复](2026-11-02-driver-pool-performance-fix.md)。

已完成实现、生产部署和复测。Driver 的取消轮询改为只读状态；Slave 目录刷新只加载 workspace 包；Run 私有包和 activation 按所属 Run 的索引读取。跨 Run 共享采用外部 agent 显式发布到 workspace、启动新实例并验证、再停止原实例的流程。

同口径客户端观测吞吐：并发 8 从 1.88 提升到 2.00 个任务/秒，并发 32 从 2.03 提升到 2.48 个任务/秒。收益有限，不能据此认为 Pool 的性能瓶颈已经解决。并发 32 复测期间，Pool 进程平均占用约一个 CPU 核心；下一步需要 CPU 剖析确定热点。

## 实现与验证

| 改动 | 行为与验证 |
| --- | --- |
| 内部 `run.status` | 一次 SELECT 验证 Driver 租约，一次 SELECT 按 Run ID 读取状态与 workspace；跳过完整 Run、事件历史、命令回执、写事务和锁。相同请求 ID 查询返回最新状态，不受完整 Run 缓存影响。真实 PostgreSQL 测试验证仅两条 SELECT，另一事务锁住 Run 时仍能读取已提交状态。 |
| `run_capability_states` | 按 `run_id` 保存私有包与 activation 投影，和 Run 在同一事务提交或回滚；能力状态未改变时不重写。读取当前 Run 不扫描其他 Run。 |
| Slave 与全局目录 | 仅从注册信息和 workspace 包表构建共享能力。私有 action 不进入 Slave 对外能力快照；全局包、activation 目录不再扫描 Run。测试检查 SQL、预热缓存后的隔离和跨 workspace 拒绝。 |
| 显式共享 | promote 将派生包写入 workspace 表，保持来源和摘要，不自动共享原 activation。使用新的 `instance_name` 激活 workspace 包，验证后通过所属 Run 的 stop 接口停止原实例。防止相同摘要与实例名导致运行时复用。 |
| 私有健康报告 | 携带所属 `run_id`，Observer 校验作用域后交由 Pool 更新。缺失或错误上下文不能访问私有包。 |
| 迁移 `011_run_capability_scope` | 一次性回填 Run 索引，将历史上已发布但仍嵌入 Run 的共享包和 activation 移入 workspace 表；保留旧 Run 快照与事件。真实 PostgreSQL 测试验证升级、作用域与原记录不变。 |

主要代码：[Pool 状态命令](../../../loom_v2/pool/tasks.py)、[资源与作用域查询](../../../loom_v2/observer/resources.py)、[数据库模型](../../../loom_v2/db/models.py)、[迁移](../../../migrations/alembic/versions/011_run_capability_scope.py)。使用流程写入[通用 skill](../../../skills/personal-computing-space/references/capability-scope.md)，生产下载包中的该文件已校验与源码一致。

相关主测试范围 `tests/api tests/driver tests/pool tests/db tests/slave tests/contracts`：739 passed、17 skipped。最终 API 作用域调整后定向回归 29 passed，角色边界与发布行为回归 6 passed。新增 PostgreSQL 查询与作用域测试验证了读写边界、缓存刷新、回滚和迁移；API 测试覆盖发布、重新激活、停止原实例的完整流程。语法、skill 校验和 diff 空白检查通过。测试清理阶段存在既有 SQLite 后台线程及弃用警告。

并行 AutoPaper 业务代码没有纳入发布；上一轮记录的 AutoPaper 生成物一致性问题不作为本轮已解决事项。生产原数据中没有 Run 私有包或 activation，因此生产迁移验证的是现有数据保持及索引回填；私有实例迁移路径由自动化测试验证。

## 生产发布

22:36–22:39 完成切换，镜像标签 `driver-pool-scope-20261102`。Observer、Pool、两个 Driver 和三个 Slave 均已更新；数据库版本为 `011_run_capability_scope`，SDK 保持 0.14.0。

| 服务 | 镜像摘要 |
| --- | --- |
| Observer、Pool | `sha256:345c29f04a7b3f360aff20b41b3214f8181d2dc9a77a5475b9e41c6193e419bf` |
| Driver primary、secondary | `sha256:7c01c4c0bc0f4c8fc2ca63e0ab3dd224cc1acc33b30962009a0cd301f910b442` |
| slave-a | `sha256:fc72d5d8244816c26fcf7c99d0f17a4e2c6cd209331966b705a0cb505b8fc2b6` |
| slave-npc | `sha256:fd685c63f303ce2516f90a6d0f13190595bfad47bf65c665f9b7e47235b9b551` |
| slave-autopaper | `sha256:ec17c630cf3f1a62656ff1970a71d558c9d6cd8973f12c43f4f814b58e285e03` |

确认无活跃执行后停写、备份并切换，保留旧镜像、配置及数据库备份。回滚脚本先停止应用、回退索引迁移，再恢复旧应用。源文件清单 SHA-256 为 `104cee0bc4715918bcb51ba2da8f54ed4c52458dc13da6b3156e8e4caa04a3cb`，上传后的三个主机均校验一致。业务能力服务镜像及数据未变更。

## 压测结果

沿用两个 Driver、三个 Slave、生产下载并独立安装的 SDK，以及单节点纯 JSON 函数。每个 Driver 4 个执行槽，Pool 提交并发上限 4；函数延迟为 0，完成状态每 2 秒轮询。输入上传和计划构建不计入阶段耗时。未增加 HTTP 自动重试或放宽超时。

| 并发 | 完成数 | 上轮任务/秒 | 本轮任务/秒 | 上轮完成 P95 | 本轮完成 P95 | 本轮提交 P95 |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 8/8 | 0.40 | 0.39 | 2.78 s | 2.67 s | 1.19 s |
| 2 | 12/12 | 0.70 | 0.71 | 3.25 s | 3.19 s | 0.78 s |
| 4 | 16/16 | 1.23 | 1.23 | 3.58 s | 3.62 s | 1.26 s |
| 8 | 24/24 | 1.88 | 2.00 | 4.85 s | 4.82 s | 1.93 s |
| 16 | 32/32 | 1.93 | 2.30 | 10.78 s | 9.62 s | 3.59 s |
| 32，完整复测 | 64/64 | 2.03 | 2.48 | 21.55 s | 16.89 s | 5.95 s |

表中吞吐与完成 P95 包含客户端轮询等待；并发 8、32 的同规模吞吐分别提高约 6.3%、22.2%。追加并发 8、64 个任务全部成功，吞吐 2.27 个/秒，完成 P95 4.30 秒。小样本结果不能当作长期容量承诺。

服务器时间戳给出另一种口径：并发 8 吞吐从上轮 2.20 到本轮 2.25 个/秒；并发 32 从 2.11 到 2.49 个/秒。并发 32 本轮入队至领取 P95 为 8.35 秒，领取至结算 P95 为 2.32 秒，提交开始至结算 P95 为 15.19 秒。这些数值不与客户端观测口径混用。

首轮并发 32 计划 64 个任务，实际启动 36 个，35 个完成；另一次调用在 Observer 的只读 `DESCRIBE drivers/default` 阶段发生 `RemoteProtocolError`，其余 28 个未启动。按原提交 ID 核对为 `not_found`，没有创建 Run。对应时段没有应用重启、OOM 或应用错误日志；根因仍未确认。相同配置完整重跑 64/64 成功，不能将复测成功表述为断连问题已修复。

监控共取得 120 次有效采样：数据库连接峰值 19，deadlocks 始终为 1（增量 0），未采样到数据库锁等待，没有其他业务 Run 同时执行。并发 32 完整复测的 5 次采样中，Pool CPU 平均 98.77%，PostgreSQL 26.56%，两个 Driver 各约 2.51%；表明 Pool 接近单核饱和，但不足以定位具体 CPU 热点。

最终生产 SQL 检查：1414 个 Run 对应 1414 条能力索引记录，`run.status` 命令回执数为 0。单次 `EXPLAIN ANALYZE` 中，按 ID 状态联查约 0.079 ms，私有能力索引查询约 0.041 ms，均使用主键索引。这是数据库执行时间，不能代表完整 RPC 延迟。

## 取消、清理与数据保持

5 秒延迟动作进入 running 后，通过公开 SDK 发起取消，约 0.47 秒观察到 Run 为 cancelled；再等待 6 秒，取消状态仍保持，未被迟到结果覆盖。该检查验证 Run 取消与结果处理，不证明远端动作在同一时刻终止。

首次取消脚本误将新的取消请求 ID 传给 `cancel(request_id=...)`，而该参数校验的是原提交 ID，因而返回 `not_found`；该 Run 正常完成。修正测试参数后使用新 Run 验证通过，服务代码未因该脚本错误改动。

本轮实际创建 257 个 Run：255 个压测 Run、首次取消脚本的 1 个正常完成 Run、1 个成功取消 Run。最终全部 closed/settled，均只有一个 Attempt；256 个保留 completed 结果，1 个保留 cancelled 结果。三个测试 activation 均确认 stopped；不可变包、结果及历史保留用于核对。

原有 1157 个 Run 的业务字段摘要、117 个包摘要和 11 个待决策 Run 保持不变。最终活跃 Run 为 0、等待数据库锁的连接为 0，连接数回落到 9。两个 Driver 的实例与 epoch（11、12）保持稳定，应用容器没有重启。Observer、Pool、双 Driver 健康检查均为 200，公开目录报告三个 Slave 和逻辑 Driver 可用。

## 证据

- 本机发布：`production/releases/2026-11-02-driver-pool-scope/`。
- 本机压测：`production/benchmarks/2026-11-02-driver-pool-scope/`，包含各档客户端计时、`server-results.json`、`server-summary.json`、`monitor.jsonl`、`monitor-summary.json`、`query-evidence.json`、断连核对、取消验证、清理及最终数据核对。
- 远端发布与备份：`/opt/loom-production/releases/driver-pool-scope-20261102/`。
- 远端压测：`/opt/loom-production/benchmarks/driver-pool-scope-20261102/`。

监控 SSH 包装器在 600 秒后超时；远端文件保留了完整 120 次采样，随后请求停止并下载核对，没有采样错误。最终核对在清理完成后单独执行。
