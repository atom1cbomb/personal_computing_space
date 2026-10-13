# 可插拔异构调度工程计划

## 阶段一：建立策略内核

1. 先为调度上下文、决策和 `round_robin_v1` 写失败测试。
2. 新建独立调度模块，提取现有轮询逻辑并保持行为不变。
3. 将策略通过 Driver 设置注入动态编排运行时。
4. 运行 Driver 动态编排测试，确认无行为回归后提交。

## 阶段二：增加异构感知

1. 为 Slave 调度画像的注册和解析写失败测试。
2. 实现 `least_loaded_v1`，使用在途任务数/并发容量评分。
3. 实现 `earliest_finish_v1`，加入服务时间和冷启动惩罚。
4. 用确定性测试覆盖容量差异、负载反转、冷热状态和同分决胜。

## 阶段三：补齐证据链

1. 扩展节点接受与重分配事件，保存调度决策。
2. 验证本地 Repository 与远程 Observer 命令路径生成相同事件。
3. 记录调度耗时并确保重放不重复作出不同决定。

## 阶段四：构建受控异构 Slave

1. 新增独立 Compose override，不改默认开发拓扑。
2. 为两个 Slave 设置不同 CPU 限额、并发容量、服务时间画像。
3. 提供环境检查命令，确认容器限额、Slave 注册画像和能力包激活状态。

## 阶段五：可重复实验

1. 定义固定请求清单和结果 JSON schema。
2. 实现只负责发起同一请求流、采集 Run/Attempt 证据的 benchmark runner；它不替 Loom 编排任务。
3. 分别运行 `round_robin_v1`、`least_loaded_v1`、`earliest_finish_v1`。
4. 输出原始结果、汇总表和实验说明，重点比较 P95 延迟、超时率与成功率。
5. 将受控异构结论与未来真实 CPU/GPU/边缘设备验证明确分开。

## 验证与提交节奏

每个阶段遵循 red-green-refactor，并形成独立提交。阶段一先运行调度单测和 `tests/driver/test_dynamic_orchestration_runtime.py`；阶段二、三增加 API/远程路径测试；阶段四验证 Compose 配置；阶段五执行完整实验后再推送分支 `feat/pluggable-heterogeneous-scheduling`。
