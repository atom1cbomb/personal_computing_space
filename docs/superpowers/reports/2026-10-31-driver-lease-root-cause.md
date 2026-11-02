# 调度延迟与 Driver 租约冲突根因及生产验证

2026-10-31，针对火山方舟联调时出现的排队延迟与 Driver HTTP 409，检查生产日志、数据库投影、
容器状态和 CPU 调用栈，并完成 Observer、Driver 两处修复及生产验证。

## 结论

两个问题相互放大：

1. **Observer 的高频健康上报路径读取了无关历史。** 查找 workspace capability package 时，
   先完整加载所有历史 Run，包括大结果；加载 workspace package 时，又读取所有包不断增长的审计日志。
   健康上报、认证等待唤醒及 Driver 注册/恢复中的重复加载，使 JSON 解码进入事件循环和执行队列事务。
   这同时拖慢任务调度与心跳响应。
2. **Driver 的租约恢复流程可能永久停止领取任务。** 心跳失败后先停止执行循环；若随后注册或
   `execution.recover` 失败，旧代码回到心跳循环。此后即使心跳成功，也不再重试完整恢复和重启执行循环。
   因而可能出现 Driver 健康探针正常、租约 active，但任务仍未被领取的状态。

生产心跳周期为 5 秒，租约 TTL 为 15 秒。第一处问题会导致租约过期，旧租约请求被拒绝，
Driver 再注册取得新 epoch；第二处问题则使某些失败后的调度无法自行恢复。

## 生产证据

- 检查时只有一个活动 Driver。实例 `driver-53a95e5c8c66` 在无容器重启的情况下多次增加 epoch；
  Observer 和 Driver 容器自 10 月 30 日启动以来均无重启，未发现第二个活动 Driver 争抢租约。
- 无业务任务执行时，Observer CPU 仍约占一个核心的 80%～89%，Driver CPU 很低。
  数据库连接主要处于 idle，未观察到与现象对应的数据库锁等待。
- 历史访问日志中，2484 次 Driver 心跳有 50 次 HTTP 409；典型失败距上次成功心跳约 20～24 秒，
  首次方舟视觉任务期间两次成功心跳之间最长相隔 **400.122 秒**。
- 数据量复核：612 个 Run 的相关 JSON 列合计 **31,995,231 字节**；最大的单个历史 outcome 为
  **10,491,221 字节**。embedding、kubernetes、autopaper 三个包的审计日志合计
  **13,859,600 字节**。没有删除这些历史记录来降低负载。
- 对 Observer 进行 35 秒、50 Hz、仅持有 GIL 时的采样，修复前取得 516 个有效样本，
  其中 239 个样本停在 JSON `raw_decode`；应用调用栈明确经过
  `record_capability_health → get_capability_package → _all_records → _load`，
  以及 workspace package 全量日志读取。非阻塞采样有丢样，因此仅用于定位调用路径，不用来替代 CPU 测量。

历史 access log 不包含 409 响应体。`stale_driver_epoch` 的判断来自租约校验代码、相同处理分支的
隔离复现及时间关系，并非声称逐一取得那 50 条响应的错误体。全部内部 409 中还可能包含其他
业务冲突，本报告不把它们全部归为租约问题。

## 代码修复

`loom_v2/observer/repository.py`：

- workspace package 优先按轻量包/激活投影解析，不再为查找 workspace 包读取全部 Run。
- 包列表读取不携带审计日志；发生变更时只读取目标包日志，持久化时不再读取一次旧日志再覆盖。
- 认证等待恢复只选择 `awaiting_auth` Run；Driver 注册和 recovery list 只选择 `thinking/running` Run，
  在数据库中过滤后才加载内容。
- 健康上报复用已解析的包，并保留激活锁内的 revision 刷新、审计历史和 report ID 幂等校验。

`loom_v2/driver/app.py`：

- 将注册、任务恢复和执行循环启动作为同一个交接流程；恢复成功后才暴露活动租约并开始领任务。
- 任一步骤失败即清除就绪租约、停止执行循环，随后重试完整交接，避免仅恢复心跳。
- 每次重注册都携带完整的 execution/orchestration 能力声明。

没有修改租约 TTL、数据库结构或公开调用契约。

## 隔离验证

新增 8 个测试用例：

- 3 个历史隔离场景分别运行于 SQLite 和真实 Docker PostgreSQL，共 6 项，覆盖终态大结果隔离、
  审计历史保留、健康报告幂等、无关认证等待不被唤醒及 Driver 活跃任务恢复。
  租约测试通过模拟历史解码消耗 20 秒来确定性复现 15 秒租约过期，不靠实际睡眠制造生产负载。
- 2 项 Driver 故障注入分别模拟心跳失败后的注册失败、任务恢复失败，检查最终恢复领任务，
  且领取前已经成功完成对应 epoch 的恢复。

在未修改的 Observer 基线上，3 个 SQLite 历史隔离测试全部失败，其中租约测试报
`stale_driver_epoch`；未修改的 Driver 上，两个恢复测试均因执行循环不再启动而超时。
修改后上述 8 项均通过。

相关回归分组结果：

- Observer 历史隔离、capability packages、凭据绑定：**31 passed**。
- Driver app、control client、execution wait、agent registry：**39 passed**。
- 扩展数据库、迁移、凭据和 Driver 检查：**56 passed、4 skipped**；跳过的是仓库中已移除的
  remote planner/provider 流程测试。

另一个扩大检查组为 63 passed、14 failed。14 个失败全部来自既有
`tests/driver/test_cleanup_continuation.py`，在提交计划时即报 `readiness_blocked`。
对未修改的 HEAD 单独运行同一文件，得到相同的 14 个失败；本次没有把这些基线失败宣称为通过，
也没有改动其业务流程或测试夹具。

## 上线与验证

只替换两个模块，分别构建并更新：

- `loom-production-observer:driver-lease-20261031`
- `loom-production-driver:driver-lease-20261031`

候选镜像从原生产镜像叠加构建，构建禁用网络，源码 SHA-256、镜像导入检查通过。
保留旧镜像、原 compose、环境文件及私有数据库备份，并配置健康检查失败自动回退。
没有重启 Slave、方舟网关或其他业务容器；切换记录验证其容器 ID 和镜像保持一致。

先单独修复 Observer 后，观察到旧 Driver 心跳持续成功、epoch 稳定，但完全没有领取命令；
测试 Run `run-93cdfa55c6c4` 保持 committed。这是第二处缺陷在生产中的对应现象。
更新 Driver 后，该 **同一个 Run** 自动被领取并完成，随后关闭；没有重新提交任务。
它包含诊断和部署期间的等待，因此不作为正常延迟样本。

随后使用相同版本的无供应商计费轻量 function，比较服务端事件时间：

| 指标 | 修复前一次样本 | 修复后三次样本 |
|---|---:|---:|
| execution submission → settled | 39.992 秒 | 8.633 / 7.686 / 7.579 秒 |
| Run opened → settled | 44.284 秒 | 11.845 / 10.335 / 10.390 秒 |
| execution claimed → settled | 35.939 秒 | 4.330 / 3.814 / 4.030 秒 |

计算本身约 1 微秒，全部输出为预期值 42，内容摘要验证通过，Run 均关闭。
客户端另使用 10 秒轮询，不能把轮询观察耗时当成服务端执行耗时；上表均来自服务端事件。
样本数量较小，是上线功能验证，不构成吞吐或长期延迟基准。

两处补丁生效后的 180 秒观察窗口中，Driver 保持同一个实例和 epoch 2212，持续领取任务；
被动观测收到 838 个 HTTP 200，没有解析到 HTTP 409，另有一次解析丢失。
访问日志复核的心跳最大间隔约 **5.261 秒**，未出现新的心跳 409。
包含任务与目录读取的 13 次 Observer CPU 采样中位数为 **5.23%**；正常空闲采样约 4%～6%，
新任务准入或全目录读取仍可能出现短时 CPU 峰值，不能把空闲水平解释为所有请求期间的上限。

公开 GRIP 复核保留修复前的全部 2339 个资源 URI，原有 ready 激活继续 ready。
火山方舟 `ark-personal` 仍为 ready/authenticated、revision 1，镜像和容器保持原样。
Observer 下载 manifest 的摘要未变，独立客户端仍使用匹配的 SDK 0.12.0。
三个从 10 月 21 日起等待用户决策的旧 Run 保留原状态，未代替用户接受或放弃。

第一轮修复移除了健康与恢复路径中的**无关**历史加载，尚保留单包日志整体读写及部分执行准备路径的
历史加载。后续已按用户要求继续修复，见下文“后续完整修复”。生产观察窗口有限，不能等同于长期稳定性保证。

## 证据与回退

本地私有证据目录：`production/releases/2026-10-31-driver-lease/`，由 Git 忽略。
包含前后采样、状态码摘要、数据库大小统计、测试日志、任务回执、公开资源快照和最终验收账本。
没有保存原始网络包，网络观测仅保存允许的状态码/错误码；未输出 API Key 或租约凭据。

第一轮生产 operator 发布目录为 `/opt/loom-production/releases/driver-lease-20261031/`，
回退镜像为 `loom-production-observer:before-driver-lease-20261031` 和
`loom-production-driver:before-driver-lease-20261031`。该轮原本只需恢复 compose 和镜像；
下述 009 迁移上线后，Observer 必须使用新版发布目录的回退脚本先降级数据库，不能直接切换旧镜像。
数据库备份只作保全，不能直接覆盖上线后的合法任务结果。

## 后续完整修复（2026-10-31）

本轮覆盖复查中列出的剩余路径：

| 路径 | 修复后的行为 |
|---|---|
| `refresh_slaves`、readiness、节点接受/派发、恢复准备 | 只查询 Run 的 workspace、包和激活投影，避免解码历史 outcome、events、draft、committed |
| 结算、认证等待/唤醒、重提、重复入队检查 | 在 SQL 中按 Run 和必要的状态过滤 execution request，不再读取整个 workspace 的历史请求正文 |
| Run-bound 包查找、聚合、激活、发布、放弃 | 查找使用轻量投影；需要变更时仅加载所属 Run，保留 digest、作用域和幂等语义 |
| REST 包列表 | 同一请求复用包/激活投影，避免逐包重新查询整个目录 |
| 启动认证恢复、陈旧执行恢复 | 先按 `awaiting_auth` 或 `thinking/running` 过滤，再加载相关 Run |
| 会话列表与详情 | 列表读取摘要列；详情只加载目标会话的 Run 和 execution request |
| workspace 包审计 | 按事件追加；report ID 通过索引查重，显式历史读取采用有限分页 |

原 `_all_records` 方法及其调用已删除。显式读取某个 Run/会话的历史，以及执行时读取当前 Run 自身状态，
仍按原业务语义保留。本次没有通过删除历史、放宽租约 TTL 或改变公开协议来隐藏负载。

`009_history_isolation` 新增 `capability_audit_events`，以 workspace、package、sequence 保存有序事件，
并为健康 report ID 建立唯一索引。包状态和新增事件在同一个执行事务中提交；并发重试复用同一报告，
不同报告均保留。嵌套操作复用事务连接，能读取本事务尚未提交的投影更新。

迁移将旧 JSON 审计逐条复制到新表，旧列保留为冻结副本。降级时将新表全部事件按顺序合并回旧列，
因此能保留升级后产生的审计记录。另新增 `runs.latest_event_at` 摘要列及 state 索引；
SQLite 测试模型与 PostgreSQL 迁移保持一致。

验证结果：

- 扩大数据库、迁移、包、凭据、GRIP 目录、Plan 投影、部署预检、Driver、agent、MCUE、会话回归：
  **157 passed、2 skipped**。跳过项是 SQLite 下不适用的 PostgreSQL 迁移/跨进程并发测试，
  相应 PostgreSQL 用例均已在真实 Docker PostgreSQL 中执行通过。
- 最后补充事务内连续上报检查后，重新运行受影响的历史隔离、包、凭据及公开投影组：
  **55 passed、2 skipped**。
- 验证覆盖：SQL 不选择历史大字段、仅解码目标 Run 的 execution request、审计分页、回滚、
  两个 repository 并发上报、迁移前后 report ID 幂等，以及升级/降级/再升级的完整历史保留。
- 扩展测试另发现 `test_dynamic_nodes.py` 的 17 个失败；在未修改 HEAD 上逐一复现，
  均属既有 readiness/pyright 夹具问题，未将其列为本轮通过项。

生产仅更新 Observer 为 `loom-production-observer:history-isolation-20261031`；Driver 保持第一轮修复镜像。
候选镜像基于原生产镜像离线叠加构建，验证 `/app` 和安装目录的四个源码文件 SHA-256、编译和导入。
升级前保存 compose、环境文件及私有数据库备份，并检查执行队列空闲。

切换后逐条核对 **17,873 条旧审计事件**与新表一致，**602 个终态 Run** 的 state/outcome/events/dynamic_nodes
摘要不变。待用户决策任务保持原样；Slave、Driver、方舟及其他业务容器的 ID 和镜像均未改变。
公开 SDK 核对原有 **2,349 个资源 URI**全部保留，原 ready 激活继续 ready；方舟仍为
ready/authenticated、revision 1，SDK 下载 manifest 摘要未变。

使用同一个轻量 function 在升级前执行一次、升级后执行三次，均返回 42、摘要验证通过并关闭 Run。
本轮比较的是“第一轮修复后”与“剩余问题修复后”，不能与最初约 40 秒的基线混为一谈：

| 服务端事件间隔 | 本轮升级前 | 本轮升级后三次 |
|---|---:|---:|
| submission → settled | 7.670 秒 | 2.894 / 2.937 / 2.936 秒 |
| opened → settled | 10.314 秒 | 3.414 / 3.449 / 3.503 秒 |
| claimed → settled | 3.919 秒 | 1.827 / 1.854 / 1.906 秒 |

Observer 切换期间 Driver 正常重新注册一次，epoch 从 2212 变为 2213；随后 180 秒窗口的
13 次采样始终为同一 Driver 实例、epoch 2213。被动观测解析到 916 个 HTTP 200、没有 HTTP 409，
有两次解析丢失；另检查自切换完成起的访问日志，877 个响应均为 200，其中 33 次 Driver 心跳成功，
最大心跳间隔 5.245 秒。上述两组统计覆盖的截止时间略有差别。

Observer CPU 采样中位数 5.62%，范围 3.85%～80.99%；高峰出现在包含公开目录访问的观察窗口内。
本轮减少的是无关历史加载与整段审计重写，不表示所有目录/准入操作均为低 CPU，也不是长期压力测试。
最终目录为 2,355 个资源，增加的六项来自三次测试各自的 Run 和 Plan。

持续运行后再次确认旧审计列摘要完全未变，新审计表为 17,875 条，新增两条报告；迁移前的每条事件
仍与新表一致。生产安装目录与本地四个发布文件的摘要一致，原有 602 个终态 Run 历史未变。
私有验证账本、任务回执、测试日志及采样保存在 `production/releases/2026-10-31-history-isolation/`，不纳入 Git。

新版生产发布目录为 `/opt/loom-production/releases/history-isolation-20261031/`，回退命令为
`python3 /opt/loom-production/releases/history-isolation-20261031/deploy_observer.py rollback`。
脚本在执行队列空闲后停止 Observer，先将 009 降回 008、合并审计记录，再恢复旧 compose 和镜像。
数据库降级失败时不会启动不兼容的旧镜像；不能用第一轮的 Observer 回退脚本跳过此步骤。

## 约 2.9 秒的进一步拆解（2026-10-31）

针对用户追问，再执行三次相同 function，并在生产宿主机被动记录内部 HTTP 的操作名、Run/执行 ID、
起止时间和状态码。仅保存白名单字段，不保存原始网络包、认证头或请求/响应正文；没有改动生产代码。
采集 90 秒，共匹配 349 个操作，无解析错误。三次任务均成功、结果摘要通过且已关闭。

**计时口径是 execution_submission → execution_settled，不是完整端到端耗时。**
本次三个样本分别为 2.906、2.874、2.958 秒，均值 2.913 秒。
从 run_opened 到入队另有约 0.50 秒的准入检查；客户端的 10 秒轮询观察延迟另算。

以下以相邻串行操作的开始时间作为边界，末段截止服务端 settled 事件，因此各段可直接相加。
各段包括少量请求间衔接开销；不能再把并行取消检查的 run.get RTT 叠加进去。

| 阶段 | 三次均值 | 证据和含义 |
|---|---:|---|
| 入队 → execution_claimed | 1,081.6 ms | 包含领取事务中的两次 readiness、启动状态和请求落库 |
| 领取后读取 Run、刷新 Slave 能力 | 360.3 ms | 其中 agents/slaves HTTP 本身约 316.6～323.6 ms |
| capability.get | 355.6 ms | 取单包时做 workspace 校验和实际查询，两处重复解析目录 |
| capability.desire | 521.1 ms | 作用域检查、设置期望状态及激活锁内刷新 |
| provision 往返 | 29.4 ms | Slave 实际处理仅 16.3～17.8 ms，其余是转发和衔接 |
| capability.health | 438.9 ms | Observer 接收 ready 报告、刷新投影及幂等处理 |
| dispatch 往返 | 102.8 ms | 两次约 82 ms，一次约 144 ms；Slave 实际处理均约 69 ms |
| run.result → settled | 23.0 ms | 结果校验、状态及执行请求结算；不包含 settled 后的 HTTP 返回尾部 |
| 合计 | **2,912.7 ms** | |

函数内部被测计算语句约 0.95～1.43 微秒，不包含 Python 启动和输入输出。Slave dispatch 的约 69 ms
已包含进程启动、输入读取/校验、执行、输出校验和保存等步骤。

### 剩余开销的来源

1. `claim_next_execution` 先调用 `_evaluate_readiness`，成功后调用 `start`，后者再调用同一检查。
   每次 readiness 自身先加载 workspace package，随后 `refresh_slaves` 又加载一次并重建能力投影。
   因此“入队到领取约 1.08 秒”不能简单称为队列等待。三个样本的 claim HTTP 请求分别在入队事件前
   243、302、505 ms 就已到达 Observer，在提交事务期间等待；入队后主要仍在处理领取逻辑。
   Driver 无任务时的 sleep 为 250 ms，这三个样本并没有等待下一轮轮询才开始 claim。
2. 前一轮已经移除 execution/outcome/audit 大字段的无关读取，但 workspace 包目录仍整体查询和
   `CapabilityPackageVersion.model_validate`。模型构造会执行 JSON Schema 校验、规范化和摘要校验；
   `_project_ready_capabilities` 还会为 ready 激活逐项扫描包目录查找。
   按上述正常调用链静态计数，入队后的主链路至少触发 12 次 workspace package 目录加载；
   这里是代码调用路径计数，不冒充函数级 tracing 的实测次数。
3. Driver 对这个已 ready 的 function 仍串行执行 get → desire → provision → health → dispatch。
   此次 provision 是幂等确认，不代表每次重新启动容器。真正的 Slave provision 很短，
   昂贵部分主要在 Observer 的重复目录构造和校验。

同时进行 70 秒、50 Hz、GIL-only、nonblocking 的 Observer CPU 采样，共取得 388 个有效样本、
231 次采样错误。135 个样本包含 `_load_workspace_packages`，102 个包含 `_evaluate_readiness`，
33 个包含 `_project_ready_capabilities`；JSON Schema 校验、摘要规范化、deepcopy 和 URI 构造均可见。
这些是相互重叠的包含关系，且采样有丢失，只用于佐证热点路径，不能换算成各函数精确耗时。
本轮表格耗时来自 HTTP 边界和持久化事件，并非 CPU 采样估算。

优先优化方向是：按目标包查询并按 digest 复用已验证的不可变内容、复用一次请求/事务内的目录投影、
合并 claim/start 的重复 readiness，以及按 revision 复用 ready 激活。激活状态、租约及权限边界仍须
保留实时校验。相比单纯缩短 250 ms 轮询间隔，这些路径覆盖了更大比例的实际开销。
没有实测优化后的延迟，故不承诺某个毫秒目标。

本轮私有证据位于 `production/releases/2026-10-31-task-latency/`，包括三个任务回执、
`http-timing.json`、`observer-cpu.raw` 和可重算的 `latency-breakdown.json`。
