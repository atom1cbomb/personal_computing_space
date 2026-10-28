# gpt-6.1-sol/max 的 skill-only Codex 复测记录（2026-10-28）

## 运行器预算更正

本记录使用的旧运行器错误地给 `codex exec` 设置了 720、900 和 1800 秒的
宿主机墙钟截止时间。那是 agent 会话的处理时间，不是远端 Run 的
`execution_timeout_seconds`，也不是本项目验收应使用的预算。被旧运行器中断的
会话不能仅因“agent 超时”判定业务失败；其远端 Run、调用结果和已写入报告仍可
独立复核，但未交付报告的会话需要在取消 agent 墙钟后的新轮次重跑。

因此下表中的“超时”是**旧运行器中断记录**，不是 Run 超时。只有公开调用返回
失败、Run 进入失败终态、越界或独立证据不满足验收条件，才构成业务失败。后续
运行器不再接受 `--timeout`，Codex 可继续等待 Run 终态、收集证据和写报告。

## 资源关系更正

原始云 VM 会话的严格失败结论中，把两个不同 URI 的 VM 当成了必须相同的资源，
属于验证解释错误。`resources/vm/9.0.16.113` 是外部接入的 `autopaper-vm`；
云管理创建的是另一条资源 URI（其云 API 返回地址为 `9.0.16.13`）。前者的
`managed_by` 为空，后者的公开描述和标签带有云管理包、区域及实例身份。两台
VM 不应因地址不同而判为关联失败；应分别按各自 resource/activation 的公开
action 和能力包 descriptor 验证，再用 descriptor 声明的 identity/relationship
字段核对云 API 结果。

此前 Codex 只比较了 activation target 和 IP，没有枚举并核对相关
`computing/vm` 资源的 `managed_by`、`package`、`state`、labels、description
及能力包 descriptor，因此没有发现这是两个资源。这是资源发现/证据收集遗漏，
不是云 VM 业务结果错误，也不是两台 VM 必须相同。更正后的重跑单独保留在本轮
证据目录，最终状态以其中的 Run、结果 digest 和报告交付为准。

更正重跑已经取得独立业务证据：外部 VM 的最终算术 Run
`run-83b4e9bdf428` 在 `slave-a` 完成，结果为
`{"start":1,"end":137,"multiple":3,"count":45,"sum_squares":282555}`；
云管理 VM 的区域、实例列表和状态 Run 分别为 `run-11ac7c9d7f98`、
`run-1d1ce000dd4e`、`run-ae6ec71f70db`，均 completed，实例身份为
`9a93d1b1-76ac-41fc-a2fc-f754fbe742fb`，区域 `bj-ceni`、地址 `9.0.16.13`、
状态 `running`。初次外部算术调用也完成了远端执行，但输出含字面反斜杠而不是
合法 JSON，已作为失败证据保留；随后用修正后的命令和新幂等键完成最终调用。

这组业务结果本身通过；但由于旧运行器在报告阶段中断，不能把本次记录直接当作
“外部 Codex 已通过”或“外部 Codex 业务失败”。需用无 agent 墙钟截止时间的运行器
重跑，最终判定只看 Run 终态、独立证据和报告交付。

本轮在 `root@9.0.3.9:22` 重新启动六个独立的 skill-only Codex 会话，模型固定为 `gpt-6.1-sol`，`model_reasoning_effort=max`。所有会话使用同一份冻结 skill、空工作区、UID 1000 隔离容器和 root 所有的凭据代理；结论只适用于本轮小样本。

## 固定输入与隔离

| 项目 | 值 |
| --- | --- |
| skill ZIP SHA256 | `ee982ea9ce2ec9e5c68fd68973566663db9de41448f5f0376fbdc126fa1b084c` |
| SDK | `loom-orchestration 0.9.1` |
| Codex | `codex-cli 0.164.0` |
| 模型 / 推理档位 | `gpt-6.1-sol` / `max` |
| 测试镜像 | `sha256:9f4ae20c9dbbc5913c16343fc316766ab50667130b805e03d3de2b9d09801c77` |
| Observer | `http://9.0.16.66:8080`，`loom.local` / `workspace-default` |
| 证据目录 | `production/skill-validation/20261028-gpt61/`（Git 忽略） |

预检通过：工作区为空、被测 UID 非 root、无 `loom_v2`、SSH 客户端、SSH agent、用户密钥目录、sudo 或 Docker socket；仅启用项目 skill。容器网络命名空间独立，IPv4/IPv6 TCP 22 出站均被拒绝。代理模式下真实 GRIP token 和模型密钥对被测 UID 不可读。七个目录的明文、Base64 和十六进制凭据扫描均为零命中；容器和验证网络结束后均已删除。

## 严格判定

| 用例 | 旧运行器状态 | 独立业务证据 | 按现行规则 |
| --- | --- | --- | --- |
| 两 Slave Map/Reduce 计算 | 正常退出 | `run-9d5b3750a407` completed，3 个节点和 3 个 attempt 的 digest 均可复核 | **passed** |
| 云区域、VM 状态与远端算术 | 720 秒被旧运行器中断 | 区域、实例、状态和两次算术 Run 均 completed；结果 digest 可复核，报告未交付 | **需无 agent 超时重跑** |
| 两集群监控对照 | 900 秒被旧运行器中断 | 8 个只读 Run completed，digest 均可复核，报告未交付 | **需无 agent 超时重跑** |
| 知识库正文与事实摘录 | 720 秒被旧运行器中断 | 集合、3 次搜索和 2 次正文读取共 6 个 Run completed，digest 均可复核，报告未交付 | **需无 agent 超时重跑** |
| 论文检索与 embedding | 900 秒被旧运行器中断 | `run-4db8f92527b9`、摘要和 embedding 结果均完成；报告已写入工作区 | **需无 agent 超时重跑/复核报告** |
| 服务包发布、检查、激活、调用、停用 | 900 秒被旧运行器中断 | 发布、check-package、激活成功；info 业务调用返回 `internal_error`；测试实例随后由操作员用公开 stop action 置为 `stopped` | **failed（调用失败）** |

“独立业务证据”用于说明远端确实发生了什么。旧运行器中断只说明当时的
agent 没有自然退出，不改变 Run 的业务状态，也不能作为 agent 处理时间预算。
需要报告交付的用例必须在无 agent 墙钟截止时间的会话中重新完成。

## 通过项：分布式计算

`run-9d5b3750a407` 使用一个业务 Run：`37..6037` 在 `slave-a`，`6038..12037` 在 `slave-autopaper`，第三个 reducer 在同一 Run 中消费两个结果。最终远端输出为：

```json
{"count":2493,"sum":15050940,"sum_squares":120770702958,"min":42,"max":12034}
```

独立客户端重新读取最终结果和两个分段结果，验证三个 content digest；独立 oracle 与远端值完全一致。最终结果引用为 `.../content/json-document/sha256/1082639e265563e3f34814e589ce56e3ae4e193f43e0ad11c61c2f024e796d37`。本例创建的 worker、reducer、orchestrator 包和三个激活没有公开删除 offer，按实际状态保留并列入台账。

## 其他业务证据

监控会话实际完成了北京与南京两集群的 8 个只读 Run：北京队列 Open、GPU 分配 2、近 30 分钟平均利用率约 11.11%；南京队列 Open、GPU 分配 1 且存在 1 个 Pending 请求、平均利用率 0%。这组数据支持“存在可见样本”，不能单独推出现在一定能调度新 GPU 作业。

知识库会话读取了两份真实正文；集合列表显示 5 个集合，`documents_info` 的正文结果和 digest 均可复核。论文会话选出 3 篇 GPU 调度相关论文，读取 Astraea 的真实摘要，并对摘要前 600 字符、完全重复文本和 35 字符园艺短句执行 3×2560 embedding；总输入 1235 字符，重复向量逐元素一致。由于旧运行器中断，报告交付仍需在无 agent 墙钟截止时间的会话中复核。

云 VM 会话独立读取的两个算术结果均为：`{"sum_of_squares":282555,"count":45,"multiples_of":3,"range_max":137}`，对应 Run `run-6391392a3f19` 和 `run-683287bdb37d`。区域和实例状态 Run 也 completed；云实例与访问 activation 的最终对应报告需在无 agent 墙钟截止时间的会话中完成，不能把旧运行器中断当作 Run 失败。

部署会话发布了 `skillval-gpt61-deploy-1d4749f3-service/1.0.0`，包 digest `3a7dcb860d73e998334e690c21c9590d20014a97ca9cfc5731a7d17c6c8aab71`，`check-package` 返回 `ready=true`，激活 revision 1 为 ready。公开 `info` action 返回 `internal_error`，随后使用 `expected_revision=1` 调用公开 `stop/v1`，最终 revision 2、state=`stopped`。没有修改既有实例。

## 证据与后续

完整原始 JSONL 保留在 9.0.3.9 的操作员目录；脱敏摘要、独立 Run 复核和凭据审计位于 `production/skill-validation/20261028-gpt61/`。本轮说明更强模型能完成严格的三节点计算；服务 `info` 调用仍有独立的业务失败。其余被旧运行器中断的会话不能据 agent 墙钟做最终业务判定，需用无 agent 超时运行器按 Run 预算重跑。

## 本地 map/reduce 编排错误排查

在本地契约测试中，修正测试进程 `PATH` 后，`orchestration_program_typecheck_unavailable`
不再出现；Pyright 阶段可以通过。随后第一次真实 Docker 执行报告
`orchestration_message_invalid`。逐帧读取 Driver 的 stdout 发现第一帧是空行，后续是
WSL 的 Docker Desktop 提示文本。该环境虽然 `which docker` 返回
`/mnt/c/Program Files/Docker/Docker/resources/bin/docker`，但 `docker info` 和
`docker version` 均以 1 退出，说明 daemon/WSL integration 不可用。这个错误来自本机
运行时，不是编排程序、远端 worker 或 reducer 的业务错误。

随后启用 Docker daemon 重跑真实容器协议：两次 map 分别在 `slave-a`、`slave-b` 执行，
reducer 在 `slave-a` 执行，三条 attempt 均完成，结果为 `{"value":80}`，调试 Run
为 `run-785f2d6f5910`。官方 map/reduce 契约测试也通过。Driver、Observer readiness
和直接执行器现在都用 `docker info` 验证 daemon 可用性；无法连接时返回
`orchestrator_runtime_unavailable`，不再把 Docker shim 的 stdout 当作 JSON 协议帧。
受影响的契约、动态节点、Driver 和编排器测试共 49 项通过。

## 无 agent 墙钟轮次

远端 `root@9.0.3.9` 随后完成了无 agent 墙钟限制的补测：`r11` 的知识库、监控、论文
和部署会话均 `agent_finished`、退出码 0，并交付了 `report.md`/`report.json`；云 VM
使用 `r10` 完成同样的补测。云 VM 报告为 passed，区域 `bj-ceni`、实例
`9a93d1b1-76ac-41fc-a2fc-f754fbe742fb`、公开 exec 输出 `{"sum":282555}` 均有独立证据。
知识库和论文报告为 passed。监控报告明确为 blocked：目录和只读查询成功，但当前
GPU 可调度能力未知，且一个 SSH activation 仍是 `pending_auth`，没有在未授权时启动
认证或提交作业。部署报告为 failed：新包发布、激活、停止均完成，但 3 次公开
`info` request-reply 调用都返回 `internal_error`，没有业务输出；实例最终已按公开
stop action 置为 `stopped`。

这些补测的独立复核仍标记为 `pending_independent_review`，因此最终验收分别保留
该状态。监控报告的资源清单已同时记录 `package`、`managed_by`、`state`、activation
和公开 attributes：外部 `resources/vm/9.0.16.113` 与云管理创建的另一台 VM 是两条
不同 URI；后者通过 `suanlicloud-http` package 及 `cloud_management_activation`、
`cloud_instance_id`、`cloud_region_id` labels 关联。两者的 `managed_by` 字段当前均为
null，不能把 null 解读为没有关系，关系必须按 MCUE/descriptor 声明的字段核对。
