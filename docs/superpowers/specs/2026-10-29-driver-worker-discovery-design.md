# Driver 在任务派发前发现新节点

遵循 [spec 设计规范](../spec-design-guidance.md)。

## 问题与目标

Driver 的 Worker 缓存首次填充后，普通任务派发不再查询节点列表。
Observer 已登记且租约有效的新 Slave 因此可能被报为
`capability_unavailable`。编排任务虽然刷新节点能力信息，但 Worker
缓存仍缺少新节点，同样无法选择或访问它。生产接入 `slave-npc` 时已复现。

目标是同一 Driver 在连续执行任务期间接纳新增 Slave，不要求重启。
普通任务和编排任务使用同一个刷新入口，复用已有 Worker 连接。

## 刷新方式

每次远程任务派发前，调用已有 `RemoteObserverRepository.refresh_slaves()`，
从 Observer 读取当前节点及租约信息，同时更新编排所需的节点能力投影。
Driver 为其中租约有效、尚无 WorkerSession 的节点创建连接，已有连接继续复用。
Worker 缓存保存连接对象，节点可用性仍由 Observer 的租约与派发校验决定。

删除“仅缓存为空才刷新”的条件及编排分支中紧接着执行的重复查询。
每次任务进入 Driver 时完成一次发现查询；无需增加定时器、刷新周期配置或依赖。
运行中编排的节点能力刷新继续使用现有实现。

## 验证

在同一 Driver 实例上，通过真实 Observer HTTP、公开 SDK 包发布和任务入口，
先在 `slave-a` 完成计算，再注册 `slave-npc` 并提交计算。
分别验证普通任务和指定新节点的编排任务完成、结果正确、实际执行目标匹配，
并确认旧节点的 WorkerSession 被复用。

测试使用进程执行器运行真实 Python 子任务；编排测试只替换 Docker 程序进程，
保留实际节点选择、能力部署、执行和结果回传。继续运行 Driver、Slave、任务提交
与编排相关测试，检查刷新改动对既有执行路径的影响。
