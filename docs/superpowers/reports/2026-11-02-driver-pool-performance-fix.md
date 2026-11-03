# Driver Pool 性能修复与生产复测

日期：2026-11-02，Asia/Shanghai。对应[修复前压测](2026-11-02-driver-pool-load-test.md)。

已修复提交查询的锁竞争、事务内重复获取连接、能力包重复校验，以及 Driver 串行等待独立 Run 的限制，并部署到生产 Observer、Pool 和两个 Driver。并发 8 的同口径吞吐从 0.40 提升至 1.88 个任务/秒，观察完成 P95 从 24.44 降至 4.85 秒。此前全部超时的并发 16 档位，本次 32 个任务全部成功；并发 32 的 64 个任务也全部成功。

原有死锁和连接池耗尽未复现。另一次持续负载中出现了两次客户端 HTTP 断连；按 request_id 核对后，一个 Run 已完成，另一个未创建。相同配置重跑 128 个任务全部成功。断连根因未确认，不将它归因于已修复的数据库问题，也不声称全部测试阶段都无错误。

## 实现

| 问题 | 修复 | 验证 |
| --- | --- | --- |
| 提交查询与领取任务反向加锁 | `get-submission` 用一条无写锁 SQL 联查执行请求、Run 状态和认证等待信息，获得同一 MVCC 快照；enqueue/resubmit 写入先锁 Run 再锁请求 | 在真实 PostgreSQL 中暂停持锁的领取事务，提交查询仍立即返回旧的完整状态，提交后再读取新状态 |
| 事务内部另开连接 | Repository 的内部读写借用当前事务会话；独立写入仍自行提交，嵌套读取不会提交外层事务 | 两条连接全部由外层事务占用时，两路内部读取仍成功；异常回滚后修改没有泄漏到数据库 |
| 新提交占用过多连接 | 每个 Pool 默认最多同时处理 4 个 `submit-plan`，等待发生在打开事务之前 | 并发 16/32 均成功，无连接池超时，Driver 租约保持稳定 |
| 全量能力包重复解码和校验 | 指定包按引用查询；批量刷新按 workspace 过滤；最多缓存 256 条完整持久化记录的校验结果，返回独立副本；activation 每次刷新 | 检查定向读取、不受无关坏记录影响、修改副本不污染缓存、发布状态/activation revision 更新可见、相同摘要下的非法内容仍被拒绝 |
| Driver 串行领取 | 每个 Driver 默认 4 个独立 Run 槽位，有空位才领取；取消和超时按 Run 隔离；租约变化释放旧执行槽 | 三个任务、两个槽位的有界并发与取消隔离测试；满槽时租约更新的恢复测试；生产耗时任务同时领取达到 8 个 Run |

主要代码：[资源与事务访问](../../../loom_v2/observer/resources.py)、[Pool 任务状态](../../../loom_v2/pool/tasks.py)、[公开任务查询](../../../loom_v2/pool/native_tasks.py)、[Driver 执行循环](../../../loom_v2/driver/service.py)。

两个配置均允许 1–64，默认 4：`LOOM_POOL_SUBMISSION_CONCURRENCY`、`LOOM_DRIVER_EXECUTION_CONCURRENCY`。详见[部署与运行说明](../../driver-pool.md)。独立 Run 槽位与编排内部 `max_live_nodes` 分别生效。

## 部署

20:26 左右完成切换，镜像标签为 `driver-pool-perf-20261102`：

| 服务 | 镜像摘要 |
| --- | --- |
| Observer、Pool | `sha256:fbea4ff1f1d5497d64e6b6fa0676f4992e20016d47f8f5635538f19cae56da06` |
| Driver primary、secondary | `sha256:e54ab80efac1415a31d6ba62a0c3b0c8f2520add89ff66027227aabd96f20475` |

确认无活跃执行后停止写入服务，保存切换点 PostgreSQL 备份，再替换应用镜像。保留原镜像及 Compose 配置，部署脚本包含失败回滚路径。无数据库迁移，数据库版本仍为 `010_driver_pool`；SDK 保持 0.14.0；Slave 和业务服务镜像未在本次发布中更新。

部署前后核对 732 个既有 Run、116 个包摘要及 11 个待决策 Run。生产阶段使用已从生产下载并安装的独立 SDK，不用仓库内 SDK 代替。

## 同口径阶梯复测

仍使用同一主机、两个 Driver、三个 Slave、相同 SDK 和单节点纯 JSON 函数。输入上传及计划构建不计时；先预热两个 Run；每 2 秒轮询完成状态，最多 4 路轮询。并发表示从提交到观察完成的在途任务数。历史记录和能力包数量较初测略有增长。

| 并发 | 本次完成数 | 修复前任务/秒 | 修复后任务/秒 | 修复前完成 P95 | 修复后完成 P95 | 修复后提交 P95 |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | 8/8 | 0.29 | 0.40 | 3.85 s | 2.78 s | 0.61 s |
| 2 | 12/12 | 0.33 | 0.70 | 6.76 s | 3.25 s | 0.77 s |
| 4 | 16/16 | 0.40 | 1.23 | 12.29 s | 3.58 s | 1.30 s |
| 8 | 24/24 | 0.40 | 1.88 | 24.44 s | 4.85 s | 1.97 s |
| 16 | 32/32 | 原 16 次调用全部超时 | 1.93 | 无有效分位数 | 10.78 s | 3.45 s |
| 32 | 64/64 | 未测试 | 2.03 | 未测试 | 21.55 s | 7.06 s |

“提交”包含 SDK 的 submit-plan 和后续提交查询；“完成”包含客户端轮询等待。并发 1、2 的观测吞吐明显受 2 秒轮询影响。服务器时间戳显示，并发 8 入队到领取 P95 从 14.46 降至 1.60 秒，领取到结算 P95 从 3.36 降至 1.52 秒。

这些是小样本阶梯测试，不是长期容量承诺。当前单 Pool 进程在并发 32 时平均占用约一个 CPU 核心，吞吐趋于约 2 个任务/秒；继续增加并发主要增加排队。修复后仍有调度、持久化、网络和进程启动成本。

## 持续负载、槽位与幂等

- 两秒函数：并发 8、16 个任务，全部成功；最多同时领取 8 个 Run。每个 Slave 的同一 activation 仍由已有执行锁保护，此测试不表示八个业务函数同时在运行。
- 首轮持续测试：计划 128，实际启动 124；客户端正常观察完成 122，另有 2 次 `RemoteProtocolError: Server disconnected without sending a response`，余下 4 个未启动。核对结果为实际创建 123 个 Run，全部完成且输出正确；另 1 个请求为 not_found。
- 相同配置持续复测：并发 16、128/128 成功，持续 69.90 秒，吞吐 1.83 个/秒，提交 P95 3.19 秒，观察完成 P95 9.75 秒。没有增加重试或放宽超时。
- 全部测试 Run 均保留一个 Attempt，结果 ticket/value 校验一致。新的复测使用新请求 ID；断连请求通过原 ID 核对，没有盲目重提。
- 查询 Run、查询提交和重复提交共 440 次 SDK 操作全部成功，重复提交均返回原 Run，没有额外创建记录。单次查询仍可能包含 DESCRIBE + INVOKE，不能将 SDK 操作数直接当作 HTTP QPS。
- 监控中 PostgreSQL deadlocks 始终为 1，即相对测试前增量为 0。主机数据库连接峰值为 19，包含所有应用；未再出现 Pool 的连接池超时。
- 两个 Driver 保持测试开始时的 instance 和 epoch（primary=10、secondary=11），未发生压力导致的租约失效。应用无重启、无 OOM；所检查的应用日志没有异常栈。

HTTP 断连只确认到客户端现象，未获得足以判定网络或连接复用根因的证据。它与本次所有已接受 Run 最终完成的结论分别记录。

## 本地验证及并行工作影响

- 主测试套件分段通过 554 项、686 项，合计 35 项 skip。
- 集成与 E2E：37 passed、3 skipped；包含真实 PostgreSQL 并发、恢复、事务及 Docker 编排验证。
- 最后一次租约槽位调整后，Driver 与 Pool 相关 27 项回归通过。
- 新的 PostgreSQL 负载回归文件 4 项通过；语法检查和 diff 空白检查通过。

完整套件并非全绿。测试过程中，另一路 AutoPaper 工作修改了搜索 schema、descriptor 和 manifest；两项独立检查失败：生成器 `--check` 报 `autopaper_http:papers-search` 过期，以及 AutoPaper descriptor 摘要不一致。对整个契约文件重检为 25 passed、2 failed。未改写这些并行工作文件，也未将 AutoPaper 业务服务变更纳入本次发布。

## 证据与收尾

最终检查通过：本次实际创建的 425 个 Run 全部完成、各有一个 Attempt，现均已关闭；三个测试 activation 已停止。原有 732 个 Run 的业务字段摘要和 116 个包摘要不变，11 个待决策任务保持不变。当前共 1157 个 Run，活跃 Run 为 0，无等待数据库锁的连接；连接数回落至 11。Observer、Pool、双 Driver 健康检查返回 200，公开目录报告两个 Driver 和三个 Slave 可用。

`cleanup.json`、`final.json`、`final-public.json` 记录清理、原数据核对及健康检查。两轮压测合计的 516 个 benchmark Run 均为 closed/settled，本次新增部分为其中 425 个；保留不可变包、结果和历史作为证据。

可复用负载脚本：[benchmark_driver_pool.py](../../../scripts/benchmark_driver_pool.py)。本次还增加了 HTTP 失败目标的脱敏记录，便于后续定位；没有添加自动重试。

本机发布证据：`production/releases/2026-11-02-driver-pool-performance/`。
本机压测证据：`production/benchmarks/2026-11-02-driver-pool-fix/`。
远端发布与回滚证据：`/opt/loom-production/releases/driver-pool-perf-20261102/`。

`*-summary.json`、`*-results.json`、`*-http.json` 保存客户端计时；`server-results.json` 与 `server-summary.json` 保存清理前的服务器状态和时间戳；`sustained-error-reconciliation.json` 保存断连核对；`monitor.jsonl` 保存 120 次监控采样；`post-ramp-health.json` 保存租约和 deadlock 对照。测试日志保留上述两项无关失败。
