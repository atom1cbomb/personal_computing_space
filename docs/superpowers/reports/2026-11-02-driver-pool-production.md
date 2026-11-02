# Driver Pool 生产部署与验收

2026-11-02 13:07（Asia/Shanghai）完成部署及最终验收。生产运行独立 Observer／Pool、两个 Driver 和三个在线 Slave，SDK 为 0.13.0，数据库版本为 `010_driver_pool`。

任务界面：http://9.0.16.66:8093/ 。Observer 的 http://9.0.16.66:8080/ 已验证跳转至 Pool。两个入口提供的 SDK wheel 均与本次构建产物的 SHA-256 一致。

## 部署范围

| 服务 | 生产位置 | 最终镜像摘要前缀 |
| --- | --- | --- |
| Observer／Pool | 9.0.16.66，8080／8093 | `6aa12607549d`，`driver-pool-20261102-r2` |
| driver-primary／driver-secondary | 9.0.16.66，8090／8094 | `8ec66e9e3c98`，`driver-pool-20261102-r1` |
| slave-a | 9.0.16.66，8081 | `5996cb2ee430`，`driver-pool-20261102-r1` |
| slave-autopaper | 9.0.16.113，8082 | `6cee4fc8c67c`，`driver-pool-20261102-r1` |
| slave-npc | 9.0.16.102，8082 | `0f2d4613c9e4`，`driver-pool-20261102-r1` |

8091、8092 已供 Slave 数据入口及 TLS 代理使用，因此生产 Pool 采用 8093；部署配置已加入 Pool 端口冲突校验。保留已有 Compose 挂载、凭据、网络与 sidecar 配置，使用现有镜像依赖离线构建应用镜像。

最终源码归档 SHA-256：`b275ca38b77766f849e4156d900e3808b63e8c6fe5a962ced63f926a3056d87b`。r2 仅补充 Pool 接管审计事件；执行端保留已验收的 r1。最终检查核对了实际容器代码摘要、SDK 版本、健康状态与自动重启策略。

## 数据保全

升级前已备份各角色 Compose、环境文件、旧镜像及 PostgreSQL，停止旧写入进程后另做切换点备份。所有数据库备份均通过 `pg_restore --list` 检查；备份与完整镜像摘要保留在各主机 `/opt/loom-production/releases/driver-pool-20261102/`。

- 619 个原有 Run 的状态、结果、冻结计划、Attempt、动态节点及原始历史摘要保持一致。
- 8,366 条原有事件逐条按序迁入 `run_events`，原事件列保留。
- 103 个原有包的内容摘要保持一致；113 个原有激活的状态、期望状态、版本与可用性保持一致。
- 11 个原有待决定 Run 保持原状；其历史恢复队列项不会阻塞新的可执行任务。
- PostgreSQL、MinIO、Registry 与既有 TLS 代理保留原容器；最终数据库无阻塞事务。

## 生产验收

提交、发现、查询、结果读取及测试资源清理使用已安装的独立 SDK wheel，通过生产公开 GRIP 接口完成。SSH／数据库仅用于部署、备份、故障注入和运维证据核对。

| 验收项 | 结果 |
| --- | --- |
| 三个 Slave 的普通任务 | 分别返回 42、44、46；结果内容及摘要校验通过 |
| 双 Driver 离线提交 | Pool 接收六个任务；相同幂等键返回同一 Run；未领取任务可取消 |
| 双 Driver 并行执行 | 六个任务分别由两个 Driver 领取；各一次领取，执行时间存在重叠 |
| 三节点 Docker 编排 | slave-a、slave-npc 计算，slave-autopaper 合并；结果 84，三个 Attempt |
| 计划与历史 | 重复提交及提交查询一致；冻结计划摘要一致；历史按两条一页连续读取 |
| 崩溃恢复，第一轮 | 强制停止 primary；secondary 接管，同 execution／Attempt，副作用计数 1 |
| 崩溃恢复，最终版本 | 强制停止 secondary；primary 接管，同 execution／Attempt，副作用计数 1；历史记录两位所有者及恢复标记 |
| 最终服务状态 | 两个 Driver、三个 Slave 在线；所有应用健康且自动重启策略恢复 |
| 清理 | 15 个测试 Run 已关闭，4 个测试激活已停止，测试计数文件已删除 |

15 个测试 Run 包含 12 个成功执行、1 个领取前取消、1 个锁等待探测和1 个验收脚本参数错误探测。后两者已留存原始证据并关闭。不可变测试包、结果与历史作为验收证据保留。

关键 Run：

- 三节点编排：`run-f9b399c00bba`。
- 最终崩溃接管：`run-95f91518bb36`，execution `execution-4b863e095079`，Attempt `attempt-484565a831c9`。历史所有者依次为 `driver-secondary`、`driver-primary`，输出 78，执行计数 1。

## 验收期间修复

1. 历史 `awaiting_decision` 恢复队列项可能占据队首：领取查询先筛选可执行 Run 状态，并保留原待决定记录。
2. Pool 持有 Run 锁调用 Observer 时，激活事务再次申请同一 Run 锁：资源事务改用自身激活锁。真实 PostgreSQL 边界测试先复现超时，再验证修复。
3. 接管更新了队列所有者但未追加 Run 历史：首次领取及恢复领取共用事务内持久化逻辑，所有权和审计事件一起提交。生产再次注入崩溃，验证接管事件可从公开历史读取。

编排验收脚本曾误将 Slave URI 填入业务资源字段 `target_resource_ref`；已改用 SDK 的 `submit_map_reduce` 和 activation 引用。该修正不涉及生产服务代码。

相关本地验证分别为：队列及部署配置 45 passed；PostgreSQL／服务边界 14 passed；激活与清理 36 passed、2 skipped；最终接管审计、服务边界与清理恢复 28 passed。两个跳过项是 SQLite 参数组合下的 PostgreSQL 专属检查；PostgreSQL 对应组合已执行。`git diff --check` 通过。

## 证据与边界

私有运维证据位于 `production/releases/2026-11-02-driver-pool/`：`acceptance.json`、各 Run 快照、源码清单、部署脚本、故障注入记录、`final-primary.json`、`final-autopaper.json`、`final-npc.json`、`final-inventory.json` 和计数文件清理记录。凭据和环境文件不进入版本库。

本次验证覆盖真实生产多机执行和两个方向的 Driver 接管。Pool、PostgreSQL 与 ContentStore 仍采用当前单实例部署；本次未测试数据库／存储故障切换，也未在生产执行降级。原配置、镜像和数据库备份可用于既定回退流程。
