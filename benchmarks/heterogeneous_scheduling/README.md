# 异构调度实验

该目录验证 Loom 的可插拔调度策略。实验分两层：

1. `run_policy_simulation.py` 是快速机制检查，用固定随机种子生成相同突发任务，比较策略在“慢节点 + 快节点”下的分配和完成时间。它不能作为系统端到端性能结论。
2. Docker 实验使用真实 Loom Driver、两个 Slave 和同一 YOLO 能力包。`docker-compose.heterogeneous.yml` 仅在一台 Mac 上用 CPU 与并发限额模拟异构；论文结论还需在真实 CPU/GPU/边缘设备上复现。

启动受控异构拓扑：

```bash
LOOM_SCHEDULING_POLICY=earliest_finish_v1 \
docker compose -f deploy/docker-compose.yml \
  -f deploy/docker-compose.heterogeneous.yml up -d --build
```

快速机制检查：

```bash
python benchmarks/heterogeneous_scheduling/run_policy_simulation.py \
  --output benchmarks/results/test_results/heterogeneous_scheduling/scheduling_simulation.json
```

正式实验必须对所有策略复用相同镜像摘要、输入清单、到达序列、超时和重试预算，并从 Run 事件与 Attempt 采集事实，不能把 Agent 的文字回复当成任务成功。

当前 YOLO 容器虽然允许多个 HTTP 请求进入，但模型推理会阻塞单个服务进程，因此调度画像中的有效容量设为 1。把 HTTP 并发上限直接当成计算容量会产生错误调度。

已完成的三轮端到端结果见 `end-to-end-results.md`。实验控制器中的固定编排程序只是构造相同的独立任务流，使策略比较可重复；它不替调度器选择 Slave，也不参与调度评分。
