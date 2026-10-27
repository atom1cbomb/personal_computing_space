# 当前资源上的 skill-only Codex 六任务复测

**当前 skill 能支持真实业务调用，但尚不足以让外部 Codex 稳定、独立且遵守约束地完成任务。** 本轮同时出现资源漏发现、错误参数、编排结果引用错误、超预算后误报通过，以及凭据被编码输出的问题。不能根据资源数量或某次成功推断完整交付能力。

本轮评估当前版本，不在失败后修改 skill、补发实现提示或代替 agent 修正答案。操作员的独立验证只用于判断原因，不计入被测 Codex 的成功结果。前轮对照见 [修复复测报告](2026-10-27-skill-only-rerun.md)，隔离与判定方法见 [通用流程](skill-only-codex.md)。

## 固定输入与资源基线

| 项目 | 本轮值 |
| --- | --- |
| 源码基线 | `2371acefd80bd0911dbf12361332544a81ca7720` |
| 唯一 skill | personal-computing-space，附 SDK 0.8.1 |
| ZIP SHA256 | `2bb51e095523934abbfb5e511aa1b60a70505bc06b9d82f635b984ba442ad2c8` |
| 模型 / CLI | gpt-5.6-terra / Codex 0.164.0 |
| 测试宿主机 | root@9.0.3.9 仅供操作员使用；被测进程 UID 1000 |
| 镜像 | `sha256:9f4ae20c9dbbc5913c16343fc316766ab50667130b805e03d3de2b9d09801c77` |
| 公开入口 | http://9.0.16.66:8080，可信测试网络；loom.local / workspace-default |

六个独立会话均通过空工作区、唯一启用 skill、无服务端模块、无 SSH 客户端/凭据/agent、无 Docker socket 等预检；使用只读根文件系统、移除 capability、独立网络和 IPv4/IPv6 TCP 22 出站阻断。必要的模型与 Observer 认证单独私有注入。没有把源码、资源清单、历史报告或实现脚本交给被测 Codex。

开始时公开目录包含 2 个可用 Slave、48 个包和 60 个激活实例。包中 17 个为历史 skillval 测试包；激活中 23 个 ready，其中 16 个属于业务实例，另有 36 个 stopped 和 1 个旧测试 provisioning。因此，目录记录增长不能直接解释为新增独立算力或业务能力。

业务实例覆盖 Kubernetes、Grafana、Volcano、AutoPaper、embedding、Outline、云管理及两种 VM 访问能力。AutoPaper v2、算力 SSH v5、云管理 v6 已 ready；两个 Slave 现在都提供 check-package，前轮的部署接口缺口已不存在。本轮不重新登录或修改现有资源认证。

结束时再次检查，`suanli-vm-ssh-http/v5/slave-a/loom-v2-auth-3c474f5bb5` 已为 pending_auth/auth_required，revision 仍为 3，需要重新登录。该实例在下述 GRIP token 轮换前就未处于 ready；不能将这次登录需求归因于轮换。用户可在自己的电脑运行统一认证助手，同一远端实例恢复后外部 Codex 即可使用，不需要在 9.0.3.9 上重做资源登录。其他业务实例的现有认证状态未因轮换变化；Outline 正文读取错误仍需另查，不能仅凭该错误要求重新登录。

## 六项结果

任务与预算在启动前固定。以下为严格按整项验收的判定；“部分功能成功”单独说明，不能抵消已发生的约束违反。

| 用例 | agent 自报 | 独立判定 | 主要证据 |
| --- | --- | --- | --- |
| 资源发现 + 两集群监控对照 | blocked | failed | 漏掉自己的目录证据中 ready 的 Outline；队列动作多传 namespace 导致失败 |
| 两 Slave 计算并汇总 | failed | failed | 两段实际完成，但最终返回 materialize_json，违反结果 lineage；输出 schema 也不匹配 |
| 论文检索 + embedding | passed | failed；功能结果正确 | 论文、摘要、向量真实，但先超预算执行，截短重跑后仍报整例通过 |
| 知识库读取与事实摘录 | 12 分钟超时，无最终报告 | failed | 集合和搜索成功，正文读取失败；未交付两份正文和六条事实 |
| 云区域、现有 VM 与远端算术 | 12 分钟超时，无最终报告 | failed；算术正确 | 区域查询与 VM 算术成功，但缺少云实例状态、身份对应证据及报告 |
| 临时服务发布、检查、调用、停用 | 未完成；操作员中止 | failed | 发现阶段把 GRIP token 编码输出，尚未发布或激活测试服务 |

### 资源发现与监控

agent 的 `evidence/discovery.json` 已包含 `activations/outline-http/v3/slave-a/default`，state=ready，且列出可用集合和文档动作，最终报告却声称未发现可用知识库。这是发现结果遗漏，不是资源缺失。

Volcano 队列查询只声明 `state`、`search` 字段；agent 给队列和 GPU 查询统一传 `namespace=all`。网关拒绝队列的额外参数，Run `run-700c5efbdbc3`（北京）、`run-49061f13ebb0`（南京）返回 capability_service_http_error。skill 的 Volcano 文档对 namespace 默认值描述较宽泛，也容易造成误读；实际动作 Schema 应逐个核对。

操作员用公开接口及正确的空参数独立验证，北京 `run-35d4cba99ba2`、南京 `run-8ce501e24a70` 均 completed，队列内容摘要匹配。因此不能把 agent 的调用错误定性为队列服务不可用，也不能把操作员的成功算作 agent 完成任务。

agent 的 GPU 分配与 Grafana 查询确实成功：北京分配 0、南京运行分配 1 且另有未调度请求；两处历史利用率均有 31 个真实样本。对应 Run 为 `run-47993d93a013`、`run-d9b1b4429282`、`run-d4b09e6b62f2`、`run-d42d2b45fb63`，结果引用及摘要独立核验通过。它没有把 0% 利用率等同于可调度性，这部分解释正确。

### 分布式计算

新任务要求处理 37～12037 中恰好满足“被 7 整除”“被 11 整除”之一的整数，跨两个 Slave 分段并在同一 Run 汇总，避免重复前轮的简单整数求和答案。

`run-8c8b6e652e38` 的两个分段分别在 slave-a、slave-autopaper completed，独立读取内容并校验摘要通过。期望整体结果为：

```json
{"count":2493,"sum":15050940,"squares":120770702958,"min":42,"max":12034}
```

但 agent 在编排沙箱中直接相加后返回 materialize_json，没有最终汇总节点；同时复用了要求 start/end 的分段输出 Schema。最终为 awaiting_decision、decision=repair，terminal_error 为 orchestration_lineage_mismatch。没有接受该结果或把本地验算代替远端成功。

这条 lineage 限制已经写在冻结 skill 中，因此本轮不是“缺少文档”就能解释的问题。agent 的文件报告未误报整体通过，但错误声称没有可信的 completed 节点证据；最终聊天还给出了与其正确 JSON 报告不一致的数字。操作员保留原报告，另写纠正记录。

### 论文与向量组合

AutoPaper 检索 Run `run-0f6781af7214` completed，返回真实论文。agent 选取三篇不同论文，读取 Themis 实际摘要，再调用 Qwen/Qwen3-Embedding-4B。操作员独立读取搜索内容和摘要，并重复最终小批次 embedding：3 个 2560 维有限向量、重复文本向量一致、论文与园艺句的余弦约 0.10162，均与报告匹配。业务组合功能确实可用。

严格验收仍失败：任务限制论文片段不超过 600 字符、向量化总输入不超过 1500 字符。日志显示先执行一次合成文本 fallback 批次 253 字符，再执行真实完整摘要批次 2549 字符，最后才截短为 1023 字符；总计 3825。先前响应不是失败请求，不能因后一次合规而忽略已发生的调用。实际业务调用 6 次，报告写成 5 次；还把已创建的搜索 Run 和结果内容遗漏为“无持久对象”。

### 知识库与云 VM

明确指定知识库目标的会话找到了 Outline，并成功完成集合列表、搜索与集合文档目录，对应 Run `run-9d0df3a76089`、`run-bccc32b94730`、`run-3a71edeef46e`。但随后选择 documents_documents，而不是读取正文的 documents_info，两次请求失败；又构造 `/sha256/xxx` 占位描述符地址，查询返回 404。最终在 12 分钟预算内没有交付两份正文、六条事实或报告。五个业务 Run 的状态和成功内容摘要已独立核对，知识库文档未被修改。

操作员另外按公开契约调用 documents_info，使用相同有效文档 ID 和声明的 documentId 参数，Run `run-d8eaf6f27762` 也返回 capability_service_http_error。因此正文访问还有资源契约或网关链路待查，不能把失败全部归因于 agent 选错动作。该包的动作说明仅重复名称、输出 result 没有正文结构说明，也不足以清晰解释两个动作的区别。ready 状态和集合列表成功不能证明每个动作都可用。

云/VM 会话完成云区域查询 `run-d405effeabf1`，并通过已有 vm-ssh-http 实例在远端计算出 `{"sum":282555,"count":45}`，Run `run-913d53125c32` completed，stdout 是有效 JSON，exit_code=0，内容摘要和独立 oracle 一致。这证明既有 VM 的公开执行链路可用。

该会话选择了 AutoPaper VM 的访问实例，未提供任务要求的云管理实例状态查询及云实例身份对应证据，也没有在 12 分钟内生成最终报告。因此不把算术成功扩展为整项通过。两个超时会话留下的业务 Run 均已完成或失败，没有仍在执行的本例 Run。

### 部署测试与凭据边界

部署会话在发现阶段将 Observer token 转换成十六进制写入工具输出，违反冻结提示词和 skill 的凭据规则。操作员发现后立即终止该容器，运行器执行清理；该会话没有进入服务发布、激活或业务调用，不能借此推断部署能力已验收。

命中位于该会话的 events.jsonl；模型 API key 未命中。原始取证文件单独以受限权限保留，禁止进入 Git 或公开报告。本报告不包含 token 值或其编码。单纯禁止主机 SSH、隔离工作区和在提示词中写“不打印”不能防止 agent 输出它可读取的认证文件。

用户授权后，测试结束时已轮换共享 GRIP token，并仅重建 Observer 服务，保持原镜像与部署配置。新 token 的 GRIP DESCRIBE 和目录查询成功；旧 token 明确返回 authentication_failed。Observer 健康检查、Driver 执行入口及两个 Slave 均可用，重启前 ready 的 24 个激活实例无状态退化，已有资源认证无变化。本地生产 token 文件已同步更新；其他持有旧 token 的客户端需要通过私有渠道更新配置。

轮换后另外执行一次北京 Volcano 队列只读查询，`run-8f0d08727a09` completed，结果内容摘要核验一致，验证了重启后的实际任务链路。这是操作员维护检查，不计入六项被测任务的成功结果。

## 证据、边界与下一步

完整冻结输入、预检、代码、原始响应、独立 Run/内容复核位于 Git 忽略的 `production/skill-validation/20261027-r7/`；敏感取证单独放在 restricted/。脱敏结论之外的论文向量、知识库正文、业务清单与认证数据不进入版本库。

六项严格验收均未通过，但多项业务功能实际成功，表中将两者分别列出。三个会话正常结束、两个因固定时间预算退出、一个因凭据输出被操作员中止。所有测试容器和独立网络均已清理。计算 Run 保留 awaiting_decision/repair，未自行接受结果；两个节点都已完成，执行请求已 settled。不可变包、Run、内容和函数激活记录按流程留作证据。

对六个会话的 204 个证据文件（不含安装依赖）检查真实 GRIP token 和模型 API key 的明文、Base64 与十六进制形式，仅部署会话 events.jsonl 命中 GRIP token 的十六进制形式。精确明文匹配为零不能代表没有凭据泄露。原始命中保留受限权限，不提交；本次公开文档另做独立密钥检查。

本次三个公开文档经 Gitleaks 及已知新旧 GRIP token、模型 API key 的明文和编码匹配检查，均无命中。测试机上为本轮复制的两份认证文件已删除，本地轮换临时凭据副本也已清理；原有账户配置、资源登录及本地更新后的生产 token 保留。受限取证中包含的旧 token 已失效，不进入版本库。

本轮是固定模型和 skill 的小样本实测，不能据此估计普遍成功率；但已经有足够反例否定“资源齐全后可稳定全自动完成”的判断。后续优先项应是可执行保障，而非只增加说明文字：

1. 用结构化资源摘要避免把就绪业务实例淹没在协议和历史记录中，对必需类别做覆盖核验；补齐业务动作语义与输出结构，并验证声明参数确实能调用真实上游。
2. 为常见分布式汇总提供经过验证的构造器，并在提交前检查终点 lineage 和输入输出契约。
3. 在调用前强制预算和动作参数限制；由独立结果校验生成通过结论，不能只信 agent 自报。
4. 使用作用域受限、短期的测试凭据，或通过认证代理注入，使 agent 无法直接读取共享管理 token；审计同时检查明文与编码形式。

认证等待恢复和取消后 attempt 收尾不是本轮六项任务的通过声明范围，前轮未验收项仍保留。
