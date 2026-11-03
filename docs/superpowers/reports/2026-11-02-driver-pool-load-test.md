# Driver Pool 生产压力测试

测试日期：2026-11-02，Asia/Shanghai。负载与采样窗口约 19:19–19:40，随后完成清理和状态核对。

**发现明显的吞吐瓶颈和并发故障。** 当前部署执行简单单节点任务时，并发从 4 增至 8，观测吞吐均约 0.40 个任务/秒，完成延迟 P95 从 12.29 秒增至 24.44 秒。并发 8 出现一次数据库死锁；并发 16 时连接池耗尽，16 个已发起调用全部在客户端超时，并影响 Driver 心跳和后台恢复循环。

已停止升压。所有实际创建的 91 个测试 Run 最终完成、各有一个 Attempt，现已关闭；三个测试 activation 已停止。原有 641 个 Run 的业务状态摘要和 11 个待决策 Run 保持不变。没有部署性能修复。

## 环境与口径

| 项目 | 本次配置 |
| --- | --- |
| 主机 | 9.0.16.66，4 vCPU；开始时约 4.9 GiB 可用内存 |
| 服务 | 单 Pool 进程、两个 Driver；与 Observer、PostgreSQL、MinIO 同机 |
| Slave | slave-a、slave-npc、slave-autopaper，三个节点轮流接收任务 |
| 发布版本 | `action-execution-20261102`；Pool 镜像摘要前缀 `318e399d58da5164`，Driver `4c5c84f73c1aef21` |
| 客户端 | 从生产下载并独立安装的 `loom-orchestration 0.14.0`，通过公开 SDK/GRIP 调用 |
| 数据规模 | 开始时 641 个 Run、11 个 awaiting_decision、0 个活跃 Run；加入测试包后 115 条 workspace package 记录 |
| 任务 | SDK 发布纯 JSON 函数，执行 `value * 2`，返回唯一 ticket；单节点冻结计划，固定包摘要和 activation revision |
| 输入准备 | 上传输入、构建计划在计时前完成；2 个预热任务不计入阶梯结果 |
| 并发定义 | 同时处于“提交到观察完成”之间的客户端任务数；不是单纯的 HTTP 并发数 |
| 完成观察 | 每 2 秒轮询 Run，最多同时进行 4 次轮询；客户端每次 HTTP 超时为 30 秒 |
| 监控 | 约每 5 秒采集主机、Docker、PostgreSQL 指标；另外采集 Pool 日志、数据库日志和 CPU 调用栈 |

这里的 30 秒是压测客户端自己的请求超时，不表示直接执行入口恢复了服务端 30 秒上限。业务函数内部耗时均值约 0.07 ms，未包含 Python 进程启动、网络、存储及调度成本。

本次是小样本、阶梯式闭环测试，用于发现瓶颈和故障；不是长时间稳态容量承诺。各阶段只有 8–24 个成功任务，P95 应结合原始样本理解。没有压测 LLM 规划、复杂编排或真实外部业务服务。

## 新任务提交与执行

| 客户端并发 | 阶段成功观察完成 | 观测任务/秒 | 提交 P95 | 观察完成 P95 | 后端现象 |
| ---: | ---: | ---: | ---: | ---: | --- |
| 1 | 8/8 | 0.29 | 1.15 s | 3.85 s | 正常 |
| 2 | 12/12 | 0.33 | 4.15 s | 6.76 s | Pool CPU 接近一个核心 |
| 4 | 16/16 | 0.40 | 5.03 s | 12.29 s | 吞吐开始停滞 |
| 8 | 24/24 | 0.40 | 6.73 s | 24.44 s | 一次 PostgreSQL 死锁，重试后完成 |
| 16 | 0/16；原计划 32，后 16 个未发起 | 不作为有效吞吐值 | 16 次约 30 s 超时 | 无有效完成分位数 | 数据库连接池耗尽；停止升压 |

“提交”计时覆盖 SDK 的 `submit-plan` 调用及随后 `get-submission` 查询；“观察完成”包含轮询等待。并发 16 阶段的客户端超时不能等同于任务未创建：恢复后按原 request_id 查询，**9 个已创建且最终完成，另外 7 个为 not_found**；没有重新提交这些请求。

从服务器持久化时间戳单独计算：

| 并发 | 从入队到领取 P95 | 从领取到结算 P95 |
| ---: | ---: | ---: |
| 1 | 1.58 s | 0.64 s |
| 2 | 2.40 s | 2.23 s |
| 4 | 5.13 s | 3.43 s |
| 8 | 14.46 s | 3.36 s |

排队延迟随并发增长。C16 最终落库的 9 个 Run，入队到领取 P95 为 34.01 秒。以上两列均来自数据库时间戳，不包含客户端轮询间隔；第一列的起点是执行请求 `created_at`，不是客户端开始发送的时间。

## 查询与重复提交

下表按一次 SDK 操作计数，不按 HTTP 请求计数；全部成功。

| 操作 | 并发 | 操作数 | 操作/秒 | P95 | 实际 HTTP 请求数 |
| --- | ---: | ---: | ---: | ---: | ---: |
| 查询已完成 Run | 1 | 40 | 7.29 | 0.138 s | 80 |
| 查询已完成 Run | 8 | 80 | 54.63 | 0.307 s | 160 |
| 查询已完成 Run | 32 | 80 | 52.79 | 0.530 s | 160 |
| 按 request_id 查询提交 | 1 | 40 | 7.57 | 0.143 s | 80 |
| 按 request_id 查询提交 | 8 | 80 | 54.80 | 0.292 s | 160 |
| 用相同 request_id、原冻结计划重复提交 | 1 | 40 | 3.65 | 0.395 s | 164 |
| 用相同 request_id、原冻结计划重复提交 | 4 | 80 | 13.55 | 0.442 s | 336 |

重复提交均返回原 Run，没有产生额外 Run。当前 SDK 在这些内置资源上会执行 DESCRIBE + INVOKE，查询通常对应两个 HTTP 请求；重复提交通常为四个，并有少量发现请求。测试反复读取同一完成记录，不能外推为随机记录或持续负载的容量。它说明主要故障集中于新 Run 的处理路径。

## 问题定位

### P0：领取与提交查询的锁顺序相反

19:24:05（并发 8 阶段），PostgreSQL 明确记录 `deadlock detected`，数据库累计 deadlocks 从 0 增至 1。两条相互等待的语句分别锁定 `run_execution_requests` 和 `runs`。

源码与 Pool 错误栈相吻合：

- [`claim_next_execution`](../../../loom_v2/pool/tasks.py) 先通过 `FOR UPDATE OF runs SKIP LOCKED` 锁 Run，再调用 `_load_execution_request` 锁执行请求；错误栈正位于这次后续加锁。
- [`NativeTasks.submission`](../../../loom_v2/pool/native_tasks.py) 在同一事务内先读取并锁执行请求，再通过 `_assert_run_workspace` 锁 Run。

Driver 后续重试使 C8 的客户端任务最终全部完成，但不能据此认为这一阶段没有故障。应统一关联记录的加锁顺序，并检查提交查询是否确实需要写锁；对真实 PostgreSQL 并发交错增加回归验证。

### P0：事务内部另开连接，饱和时拖累心跳和恢复

并发 16 阶段，Pool 日志出现：

```text
QueuePool limit of size 5 overflow 10 reached,
connection timed out, timeout 30.00
```

错误栈包括 `heartbeat_agent → list_agents` 和 Pool 后台恢复循环。数据库整体连接数峰值为 20；这是所有应用的合计，不能与 Pool 自身的 15 个连接上限混为一谈。

[`make_engine`](../../../loom_v2/db/session.py) 未单独配置连接池容量。另一个关键因素在 [`ResourceRepository`](../../../loom_v2/observer/resources.py)：`_execution_transaction` 持有事务连接，`_execution_write_session` 能复用它，但 `list_agents`、`_agent_record` 等方法仍通过 `self.sessions()` 新建会话。领取流程的 `_evaluate_readiness → refresh_slaves → list_agents` 会进入这类嵌套读取路径。

在本机一次性 PostgreSQL 容器中，使用实际 `PoolRepository`、2 个连接、0 overflow、0.3 秒超时进行隔离复现：两个并发 `_execution_transaction` 各持有一个连接，再调用 `list_agents`，两者均等待新连接并超时；单独调用 `list_agents` 在前后都成功。这确认了嵌套取连接的饥饿机制。生产日志确认了连接耗尽及心跳受影响；本次没有逐事务重放生产现场，因此不把所有 C16 等待都归因于单一调用点。

应优先复用事务会话、缩短持有连接和行锁的时间，并为提交入口增加有界并发和清晰的过载反馈，保障心跳与恢复能继续工作。单纯扩大连接池无法消除这种等待机制。

### P1：反复加载和校验全部能力包占用 Pool CPU

C4 阶段 Pool CPU 平均约 91%、峰值 99.65%；C8 平均约 89%、峰值 99.35%。Docker 的 100% 对应一个 CPU 核心，主机共有 4 核。同期两个 Driver 各自平均 CPU 不到 1%，PostgreSQL 平均约 8%–11%。采样窗口内可用内存最低仍约 4.68 GiB，没有发现内存耗尽迹象。

随后在 C4 下进行两次有界诊断复测，12 个和 8 个任务全部完成，吞吐分别为 0.38、0.41 个/秒。它们不计入上面的基准表。

第二次采用 `py-spy 0.4.1`、50 Hz、30 秒、`--gil --nonblocking`，得到 592 个持有 GIL 的样本。其中：

- 248/592（41.9%）包含 `_load_workspace_packages`。
- 237/592（40.0%）包含 `CapabilityPackageVersion.from_record`。
- 80/592（13.5%）包含 `_find_package`。

这些是包含子调用的栈样本占比，彼此有重叠，不能相加；也不代表所有本地扩展和 I/O 的精确 CPU 时间。它们足以确认包加载、反序列化、Schema 校验、URI 规范化是明显热点。

[`_load_workspace_packages`](../../../loom_v2/observer/resources.py) 当前查询全部 workspace package 行，逐条 `from_record` 并校验 activation；即使查询一个包，`get_capability_package` 也先执行全量加载。readiness 路径还会再次加载。建议改为按 workspace 和包定位，按不可变摘要缓存已验证内容，按 revision 更新 activation，减少热路径中的全量扫描和重复校验。

### P2：每个 Driver 的领取循环串行等待独立 Run

[`DriverService.run_execution_loop`](../../../loom_v2/driver/service.py) 领取一个 Run 后，直接 `await _execute_and_wait_remote(...)`，完成后才继续领取下一个。正常 C2–C8 阶段同时持有的 Run 数最多为 2，与两个 Driver 对应。

这是独立 Run 的并发限制，不表示一个编排 Run 内不能并行调用多个节点。C16 恢复期间按记录时间区间会短暂得到 3 个重叠区间，包含所有权恢复的影响，不能解释为三个执行槽。

可在解决 Pool 的锁、连接和 CPU 问题后，为每个 Driver 引入有界的独立 Run 执行并发，并验证领取、租约、取消、恢复语义。现阶段直接增加 Driver 数量可能进一步放大 Pool 压力。

## 清理与生产状态

全部业务负载由公开 SDK 发起；SSH 只用于指标、日志、采样和只读数据库核对。没有重启应用容器、修改应用配置或发布性能修复。

- 91 个测试 Run = 2 个预热 + 69 个阶梯测试实际创建 + 20 个 CPU 诊断复测；全部完成且输出 ticket/value 正确，各一个 Attempt。
- 91 个 Run 已关闭，91 条执行请求均 settled；三个测试 activation 均 stopped。保留不可变包、结果和历史作为证据。
- 原有 641 个 Run 的 state、outcome、draft、committed、attempts、dynamic_nodes 摘要均未变化；原有 11 个 awaiting_decision Run 集合未变化。
- 当前总计 732 个 Run，活跃 Run 为 0，没有等待数据库锁的连接；数据库连接数回落至 11。
- Observer、Pool、两个 Driver 的 `/healthz` 均返回 200；公开资源报告 active_drivers=2，三个 Slave 均 available。
- 主机监控期间未发现其他活跃业务 Run。应用容器启动时间早于本次测试，重启计数为 0。

## 复现与证据

可复用脚本：[`scripts/benchmark_driver_pool.py`](../../../scripts/benchmark_driver_pool.py)。它支持 setup、batch、api、reconcile、cleanup；每个阶段使用唯一 label。计时前准备输入和计划，发生异常停止启动后续任务，保存请求 ID 以便超时后核对。一次测试只运行一个写入该输出目录的进程。

使用已安装 SDK 的 Python，配置文件包含 `observer`、`pool`、`authority`、`workspace`、`token`，凭据文件不提交到仓库。例如：

```bash
BENCH_PYTHON=production/releases/2026-11-02-action-execution/client-venv/bin/python
BENCH_CONFIG=production/releases/2026-11-02-action-execution/client-config.json
BENCH_OUT=production/benchmarks/driver-pool-next

"$BENCH_PYTHON" scripts/benchmark_driver_pool.py setup --config "$BENCH_CONFIG" --output "$BENCH_OUT"
"$BENCH_PYTHON" scripts/benchmark_driver_pool.py batch --config "$BENCH_CONFIG" --output "$BENCH_OUT" --label warmup --count 2 --concurrency 1
"$BENCH_PYTHON" scripts/benchmark_driver_pool.py batch --config "$BENCH_CONFIG" --output "$BENCH_OUT" --label c2 --count 12 --concurrency 2
"$BENCH_PYTHON" scripts/benchmark_driver_pool.py api --config "$BENCH_CONFIG" --output "$BENCH_OUT" --label read-c8 --operation read --count 80 --concurrency 8
"$BENCH_PYTHON" scripts/benchmark_driver_pool.py reconcile --config "$BENCH_CONFIG" --output "$BENCH_OUT"
"$BENCH_PYTHON" scripts/benchmark_driver_pool.py cleanup --config "$BENCH_CONFIG" --output "$BENCH_OUT"
```

目前已知 C16 会影响心跳，修复前不应重复升至该档。发现超时应先停止升压，待服务恢复后按 request_id 核对；客户端超时并不能证明服务器未接受。`STOP` 文件可阻止新任务启动，已启动任务会继续收尾。cleanup 会重新查询缺失的回执，但 not_found 只代表查询时刻的状态，需确保服务已恢复、原请求已结束再作最终清理核对。

本次本机私有证据位于 `production/benchmarks/2026-11-02-driver-pool/`，远端位于 `/opt/loom-production/benchmarks/driver-pool-20261102/`。关键文件：

| 文件 | 内容 |
| --- | --- |
| `noop-c*-summary.json`、`*-results.json`、`*-http.json` | 客户端分位数、逐任务时间、脱敏 HTTP 计时 |
| `server-results.json`、`server-summary.json` | 清理前的服务器队列、Attempt、结果及分阶段统计 |
| `timeout-reconciliation.json` | C16 的 9 个完成 Run 与 7 个 not_found |
| `monitor.jsonl` | 231 次主机、容器与数据库采样 |
| `pool-pressure.log`、`postgres-pressure.log` | 连接池超时、死锁错误和 SQL |
| `pool-gil.stacks`、`profile-summary.json` | GIL CPU 样本和热点统计 |
| `reproduce_nested_sessions.py`、`nested-session-reproduction.json` | 一次性本地数据库的连接饥饿复现 |
| `cleanup.json`、`final.json`、`final-public.json` | 清理结果、原数据摘要比较及最终健康状态 |

脚本已通过语法检查、CLI 检查，并实际执行 setup、batch、api、超时核对和 cleanup。生产性能修复及修复后的回归压测尚未实施。
