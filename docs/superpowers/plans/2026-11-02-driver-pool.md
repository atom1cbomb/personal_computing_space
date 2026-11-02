# Driver Pool 与资源直连实施计划

遵循 [Spec 设计规范](../spec-design-guidance.md)。本次直接更新服务契约，不保留旧执行中继作为任务必经路径。

## 目标与边界

Observer 提供资源目录、包与 activation 管理、Slave 注册和健康投影。独立 Driver Pool 服务拥有 Run、Plan、Node、Attempt、提交幂等、执行队列、任务校验、结果验收与历史。多个 Driver 从 Pool 领取任务，按受信 Slave binding 直连执行。Run/Plan 的公开动作绑定 Pool；Observer 可作为统一发现和任务入口转发请求。

复用 PostgreSQL、SQLAlchemy、现有 GRIP 契约和 ContentStore。首版共用 PostgreSQL 实例，按服务划分写入职责；资源目录可以读取任务摘要与 Run-bound 包投影，不读取任务历史。Pool 中的资源变更通过 Observer 的内部资源接口完成；Run-bound 对象随 Run 由 Pool 管理。

## 实施步骤

1. 提取任务 repository、原生任务动作和准入校验到 pool 模块；增加 Pool 应用、配置和内部客户端。Observer 将任务操作交给 Pool，删除内部 Driver 执行命令与节点执行中继路径。
2. 支持多个 Driver 注册；同一 Driver 身份的新实例替换旧实例，其他 Driver 保持活动。领取通过 PostgreSQL 行锁竞争，任务写入按 Run 串行。恢复只接管所有者租约已失效的任务。
3. Run 历史改为追加事件表，状态与事件同事务提交；提供有界历史读取和数据迁移。保留不可变计划与执行证据，迁移不删除历史。
4. Driver 控制命令连接 Pool；资源发现返回受信直连 binding。普通任务和编排节点共用 WorkerSession 直连路径。Slave 在执行前向 Pool 核实 Driver 租约与 Attempt 所有权，并检查 workspace、目标、activation revision。超时恢复使用同一 Attempt 查询/重放本地账本。
5. 更新 GRIP 绑定、Compose、部署生成器、SDK/skill 文档。Pool 的提交与查询可独立于执行 Driver 工作，Driver 实例可以横向增加。
6. 验证公开提交到真实 Slave 的普通任务和动态节点执行、多 Driver 领取互斥与并行、失效所有者隔离、恢复、提交幂等、历史分页与迁移。运行相关现有回归；记录基线失败和未验证环境。

## 一致性要求

- 提交幂等、计划冻结与入队在同一个任务事务中完成。
- Pool 是任务状态唯一写入方；Observer 是 workspace 资源状态唯一写入方。
- 每个 Run 同时只有一个有效执行所有者；旧实例不能继续派发或修改结果。
- Slave 账本中的已执行 Attempt 在网络重试、Driver 重启后保持同一身份。外部副作用未知时按原有 repair/reconcile 语义处理。
- 静态内容验证可以复用；租约、所有权与 activation revision 在执行边界检查。
- 认证恢复通过持久状态重新检查，不能依赖仅在内存中的通知。

## 交付记录

已完成以上 6 步。主要落点：

- `loom_v2/pool/`：独立应用、任务 repository、准入校验、原生任务动作、恢复循环及固定方法集的内部 RPC；Observer repository 只保留资源职责与任务转发。
- `loom_v2/driver/` 与 `loom_v2/slave/`：Pool 控制通道、可信 Slave binding 直连、派发前授权、Attempt 持久账本和重复执行隔离；删除 `node.execute` 与 Observer 执行中继路由。
- `010_driver_pool`：多 Driver 租约与追加事件表；提供按页读取、升级与降级历史保全。PostgreSQL 按 Run/请求身份锁定，领取使用 `SKIP LOCKED`。
- Compose、部署生成器与 `dev-up.sh` 增加 Pool；远程部署检查 Observer 和 Pool 健康。任务 Web 页面由 Pool 提供，支持事件翻页。
- SDK 升至 0.13，`run()` 默认只取状态；历史显式分页。更新外部 agent skill、示例和 [部署升级文档](../../driver-pool.md)。

验证范围包括：独立 Observer/Pool repositories 的 ASGI HTTP 边界与真实 Slave 子进程执行、普通任务及动态节点直连、真实 PostgreSQL 多 Pool 实例并发领取/幂等/接管、旧所有者隔离、Pool 后台恢复、Slave 重试/重启账本、历史迁移与分页、SDK wheel 与 skill 打包、部署生成和 Web 入口。动态编排边界测试使用可控沙箱调度器，节点仍走真实 Slave 执行路径。

验证结果：

| 检查 | 结果 |
| --- | --- |
| `pytest tests/api tests/driver tests/db tests/slave tests/contracts tests/deployment tests/observer tests/pool tests/integration/test_repositories.py tests/integration/test_conversations.py tests/integration/test_io_schema_e2e.py -q --tb=short` | 793 passed，17 skipped |
| 最后一次锁顺序调整后：`pytest tests/pool/test_concurrency.py tests/pool/test_service_boundary.py tests/api/test_agent_registry.py tests/driver/test_control_client.py -q` | 35 passed |
| Web 迁移后：`pytest tests/web tests/pool/test_service_boundary.py tests/api/test_observer_run.py tests/deployment -q` | 81 passed |
| `python -m loom_v2.cli --self-test` | completed，输出 `value=6` |
| Compose config、Shell/JavaScript 语法、Python compileall、`git diff --check` | 通过 |

测试使用项目虚拟环境，并把 `.venv/bin` 加入 PATH，以调用固定版本的 Pyright。

后续按用户要求完成生产部署及真实多机验收：两个 Driver、三个 Slave，验证离线提交、并行执行、三节点 Docker 编排和双向崩溃接管；保全 619 个原有 Run、103 个包、113 个激活及 11 个待决定状态。生产验收暴露的激活事务锁和接管历史遗漏已修复并复测。详见[生产验收记录](../reports/2026-11-02-driver-pool-production.md)。首版仍共享数据库和 ContentStore。
