# 仅凭 skill 使用算力空间的外部 Codex 验证流程

## 验证目标与结论范围

验证一个全新 Codex 能否仅凭 `personal-computing-space` 发布版 skill，从公开接口发现资源、理解契约、完成任务，并提供可独立复核的执行证据。被测 Codex 不获得 Loom 源码、部署记录、预先整理的资源清单、任务实现脚本或任何资源主机的 SSH 权限。

这是一项外部使用验收，不以开发者自己写 SDK 程序成功、已有单元测试通过或 Codex 声称“完成”代替结果。一次验收固定 skill ZIP、SDK、Codex 版本、模型、任务和预算；中途更换 skill 或修复服务后，应另开一轮，保留原失败记录。

本次指定测试入口为 **`root@9.0.3.9:22`**。root 仅属于测试操作员；被测 Codex 在该机器的非 root 容器中运行。其他环境可以复用同一流程，替换测试机和公开 Observer 配置。

## 输入与权限

| 输入 | 来源与边界 |
| --- | --- |
| 唯一项目知识 | `scripts/build_agent_skill.py` 生成的 `personal-computing-space.zip`，包含完整 skill 和独立 SDK wheel |
| 任务 | 自然语言目标、数据生成规则、资源/时间预算、允许的副作用、验收标准；通过 stdin 输入 |
| 算力空间连接 | Observer URL、GRIP authority、workspace ID；凭据以私有 token 文件注入 |
| Codex 认证 | 独立的 Codex `auth.json`，或下述最小模型服务配置；不复制宿主机整个 `.codex` 或其中的配置与历史 |
| 基础工具 | 固定版本 Codex、Python 3.12、pip、Git、shell 和通用系统库 |

地址和认证是连接服务所必需的启动配置，不是额外的项目知识。若这些输入缺失，记录 `blocked_environment`，不要让 agent 从宿主机文件中寻找。SDK wheel 属于发布版 skill 的附件；SDK 安装与调用代码由被测 Codex 根据 skill 完成。

不向容器提供 `.ssh`、SSH agent socket、kubeconfig、云密钥、Docker socket、模型服务账号之外的宿主机环境变量、项目 checkout、其他 MCP/plugin 或其他启用的 skill。由已授权能力包在远端执行 SSH 是公开资源动作的一种实现，不等于向被测 Codex 授予主机 SSH 权限。

## 操作员准备

### 1. 固定发布物

在开发机的项目根目录构建；不要把 checkout 复制到被测工作区：

```bash
python scripts/build_agent_skill.py --output dist/skill-validation
sha256sum dist/skill-validation/personal-computing-space.zip
git rev-parse HEAD
```

记录提交号、ZIP 摘要和 wheel 版本。ZIP 必须包含一个独立 `loom-orchestration` wheel，其中没有 `loom_v2` 服务端模块。部署运行着哪个服务版本，应由操作员另行记录，不能只拿本地 HEAD 当作生产版本。

测试宿主机需要 Linux、Docker、Python 3、`nsenter`、`iptables`、`ip6tables`。以下文件只进入操作员目录，不作为 agent 上下文：

```bash
ssh -o ForwardAgent=no root@9.0.3.9 \
  'install -d -m 700 /root/loom-skill-validation /root/loom-skill-validation/image'
scp dist/skill-validation/personal-computing-space.zip \
  scripts/run_skill_validation.py root@9.0.3.9:/root/loom-skill-validation/
scp deploy/skill-validation/Dockerfile \
  root@9.0.3.9:/root/loom-skill-validation/image/
```

在测试机上构建基础镜像：

```bash
cd /root/loom-skill-validation
docker build --build-arg CODEX_VERSION=0.164.0 \
  -t loom-skill-validation:codex-0.164.0 image
```

本次 `9.0.3.9` 未启用默认 Docker bridge，构建镜像时需在上面的 `docker build` 中加 `--network host`。这只用于操作员的构建过程；运行器为每个被测容器创建独立 bridge 网络，结束时删除，不依赖默认 bridge 或使用 host network。

首次建镜像需要访问镜像和 npm 仓库；agent 安装 SDK 依赖需要 Python 包源。这些可在操作员侧预缓存通用依赖，但不能添加任务答案、项目源码或业务实现。保存镜像 ID 后，同一轮使用同一镜像。CLI 版本变更后重新运行下面的预检。

### 2. 先验证隔离，不调用模型或业务资源

```bash
python run_skill_validation.py \
  --bundle personal-computing-space.zip \
  --output evidence/preflight-001 \
  --preflight-only
```

[运行器](../../scripts/run_skill_validation.py) 为每次执行创建独立容器，使用 UID 1000、只读根文件系统、全部 capability 移除、`no-new-privileges`，不使用 host network/PID。仅挂载新的空 `/work`；home 和临时目录使用临时文件系统。

操作员通过 `nsenter` 仅进入该容器网络命名空间，阻断 IPv4/IPv6 的 TCP 22 出站；资源机若使用其他 SSH 端口，逐个追加 `--ssh-port PORT`。运行器先比较宿主和容器的网络命名空间，拒绝 host network 或远程 Docker daemon，避免更改宿主网络。被测进程无权撤销这些规则。

预检确认：工作区为空、UID 非 root、没有 SSH 客户端/agent/用户密钥目录、没有 sudo/Docker socket、无法导入 `loom_v2`。通过 Codex `skills/list` 获取实际目录，关闭所有其他 skill，重启目录查询并确认**唯一启用项是本项目 skill**。CLI 自带但已禁用的系统 skill 属于运行时文件，不作为启用的技能上下文。仅更换 `CODEX_HOME` 不足以满足这一条件。

`preflight.json` 保存目录与隔离证据。预检失败时停止测试；不能以提示词“请不要读其他文件”替代隔离。`--preflight-only` 通过不表示模型、Observer 或业务功能可用。

### 3. 准备连接与认证

操作员通过已有安全渠道准备私有文件，例如：

```text
/root/loom-skill-validation/private/grip-token
/root/loom-skill-validation/private/codex-auth.json
```

目录权限为 0700，文件为 0600。这些文件既不进入 Git，也不进入 skill ZIP、任务提示词或执行报告。不要复制宿主机全部 Codex 配置。

使用自定义模型服务时，将 `--codex-auth-file` 替换为 `--codex-provider-file private/codex-provider.json`。该私有 JSON 只允许 `model`、`base_url`、`wire_api`（固定为 `responses`）和 `api_key` 四个字段；由操作员从已授权的模型配置中提取。运行器拒绝额外配置和 URL 内嵌凭据，把密钥写入容器临时 home，在启动 Codex 时读取，并从其工具子进程环境中排除该变量。报告只保留模型和服务地址，不复制宿主机的 skill、MCP、插件或历史。

对 HTTP Observer，明确限定为操作员批准的可信测试网络；agent 依据 skill 显式启用 SDK 的 HTTP 支持。公网部署使用 HTTPS。需要服务直连时，可信 endpoint/audience 必须来自操作员配置和 DESCRIBE，不能让 agent 猜地址或绕过证书验证。

## 任务集合

开始前就确定哪些用例必测、哪些依赖可选资源；不要看到失败后把必测项改成可选。每个用例独立启动容器和 Codex 会话，不能继承上一例的源码或发现缓存。测试操作员可以在外部保留资源清理台账。

| 编号 | 业务任务 | 通过条件 | 前提 |
| --- | --- | --- | --- |
| T1，必测 | 发现可用计算与服务资源，给出可执行任务及限制 | 从公开接口取得 Slave、包、资源、实例及契约证据；区分发布/在线/ready/认证/服务入口 | Observer 和 GRIP 连接可用 |
| T2，必测 | 在一个算力空间执行节点上计算 1～10000 的数量、总和、平方和、最小值、最大值 | 真实 Run 完成；返回可读取的结果引用；attempt 的执行目标属于发现的远端 Slave；数值全部正确 | 至少一个支持相应执行器的 Slave，Driver 可用 |
| T3，多节点 | 将同一序列拆成两段，在两个不同 Slave 计算后汇总 | 同一个 Run 中有两个不同 Slave 的实际 attempt 和汇总依赖，结果与 T2 一致 | 两个满足执行要求的可用 Slave |
| T4，服务调用 | 选择一个已就绪的论文、监控、集群或模型服务完成只读查询 | 依据发现的 action/Schema 调用；保留服务结果、时间、参数和完整性；按运行契约核验 Run 或直接结果 | 预先选定服务类别存在；认证有效 |
| T5，幂等与取消 | 提交短暂等待任务，重试同一提交，运行后取消 | 相同 request_id/内容未生成重复任务；按同一 Run 查询取消终态；确认剩余节点/资源状态 | 公共提交和取消能力可用；仅操作本次任务 |
| T6，认证恢复 | 对专门的测试实例完成一次需用户参与的认证，再继续原任务 | 等待记录与实例/修订一致；用户按统一入口登录；同一 Run 恢复且不重复创建资源 | 人已就绪，授权创建测试实例或使用指定实例 |
| T7，包部署 | 通过公开接口发布、检查、激活一个最小服务包并调用 | 不使用节点 SSH；镜像 digest、check-package、activation、业务结果和停用证据完整 | 明确的 registry 推送权限、已接入服务入口的 Slave |

T4 的实例可以是 AutoPaper 论文查询、Grafana 指定时间窗口的 GPU 查询、Volcano 队列查询，或现有 embedding 服务的两条文本输入。由实际发现结果决定 action、参数和结果解释。资源目录不能替代一次真实业务调用；没有采样不能解释成零利用率，分配时长不能解释成实际计算量。

GRIP offer 的 `mode=request-reply` 描述协议交互方式，不能据此断言业务执行不产生 Run。结合 action 的 `output_schema`、包的 `runtime_binding.interaction` 及实际响应判断：`task` 要核验 Run 终态和输出；直接请求回复或流式业务要核验对应结果。遇到未知字段或不完整契约时保留不确定性。

T5 只使用本次创建的任务；明确授权取消，不中断其他用户任务。T6 不通过撤销现有生产登录来制造故障。T7 缺 registry 授权时记录 blocker，不索取节点 SSH 来安装镜像。真实 VM 创建/销毁属于额外生命周期用例，必须事先给定数量、费用、期限、命名及清理范围，不能由基础验收默认触发。

## 给被测 Codex 的任务提示词

将下面内容作为一条任务消息；仅替换“本例任务”和范围，不添加 SDK 示例、资源 URI 清单、历史问题解决办法或参考答案。环境中的 `LOOM_*` 变量和 token 文件路径是启动配置。

```text
请使用 $personal-computing-space 完成下面的真实算力空间任务。

你只有已安装的项目 skill 和空工作区。Observer 地址、GRIP authority、workspace
在 LOOM_OBSERVER_URL、LOOM_GRIP_AUTHORITY、LOOM_WORKSPACE_ID 中；
访问凭据位于 LOOM_GRIP_TOKEN_FILE 指向的文件。凭据只能用于客户端认证，
不要打印、写入任务源码/公共内容、提交到版本库或收入报告。

自行阅读 skill、安装随 skill 分发的 SDK、通过公开接口发现资源并读取调用契约。
不要访问资源主机 SSH、内部控制接口，也不要下载本项目源码或查找其他机器上的
示例/历史会话。工作区中由你本次生成的代码和证据可以继续使用。

本例任务：在算力空间的一台执行节点上，计算整数 1 到 10000（含端点）的
数量、总和、平方和、最小值和最大值，取回结果并验证。

允许上传本例计算程序/契约、创建带唯一验证前缀的包和 Run。
自行生成本例唯一前缀，将其用于 request_id、幂等键和创建对象名称；
同一操作重试复用原键，不同轮次不得复用。
最多 1 个业务 Run、4 个执行节点、2 个并发节点，任务执行限时 300 秒。
不创建收费 VM，不修改已有业务资源、服务配置或认证。
你可以在本地生成输入和验算，但实际计算必须有算力空间远端执行证据。

总工作时间限 15 分钟。遇到缺资源、认证、接口或就绪条件时，输出具体 blocker
及公开接口证据，不通过 SSH 修复，不虚构结果，也不要放宽原验收标准。
需要用户认证时明确说明并等待，不自行接受需要人工判断的成功结果。

在工作区写 report.json 和 report.md，报告 status（passed/failed/blocked）、
资源与 action URI、request_id/Run/结果引用、核验结果、创建对象与清理状态。
请求回复或字节流动作没有 Run 时，明确说明调用类型并保留业务结果证据。
原始响应可在去掉凭据后保存到 evidence/；你的最终报告须与证据一致。
```

将其存为操作员目录下的 `task.txt`，运行：

```bash
python run_skill_validation.py \
  --bundle personal-computing-space.zip \
  --output evidence/t2-001 \
  --prompt-file task.txt \
  --observer http://9.0.16.66:8080 \
  --authority loom.local --workspace workspace-default \
  --grip-token-file private/grip-token \
  --codex-auth-file private/codex-auth.json \
  --timeout 900
```

此地址来自本次工作空间的部署配置；复用时替换为实际公开地址。运行器默认采用该 CLI 的默认模型；如果验收固定模型，显式传 `--model` 并记录实际启动模型，不从操作员用户配置隐式继承。每次 `--output` 必须是新目录，避免污染上一轮证据。

运行器把提示词通过 stdin 交给 `codex exec`，关闭插件、apps、web search 和子代理，使用临时会话。完整 JSONL 和 stderr 由宿主机保存，不挂入容器；`/work` 保存 agent 自己生成的内容。`agent_finished` 只表示 Codex 正常退出，业务验收始终等待独立复核。Codex 非交互执行、JSONL 和输出文件用法见 [OpenAI 官方文档](https://learn.chatgpt.com/docs/non-interactive-mode)。

## 独立复核

操作员使用**另一个公共 API 客户端**复核，不能只读 agent 的汇总文件，也不需要 SSH 到 Observer/Slave：

1. 检查 `preflight.json`、skill 哈希、镜像 ID、CLI 版本、实际模型、起止时间及完整提示词。目录包含其他启用 skill、源码或既有文件时，本轮验收无效。
2. 从 agent 报告取得 request_id/Run URI，再独立查询 Observer 的提交、Run、attempt 和结果引用。确认执行目标、节点数量、状态与输出 lineage；执行事件应落在本例时间范围内，防止固定幂等键返回旧 Run。不得把本地算出的正确数字冒充远端结果。
3. 通过返回的内容引用取回原始结果，核对服务提供的内容摘要。引用及其 digest 以 Observer 为准，不为了通过验证而改写引用。
4. 对确定性计算使用独立 oracle。例如 T2/T3 的期望值为 `count=10000`、`sum=50005000`、`sum_squares=333383335000`、`min=1`、`max=10000`。T3 另核对两个不同 Slave 的实际执行和汇总关系。
5. 对动态服务结果，检查查询参数、时间窗口、来源、非空/缺失状态及语义约束；必要时另发一次只读查询交叉确认。动态数据不要求两次响应逐字一致，单次“HTTP 200”不能证明业务完成。
6. 检查命令日志和生成代码是否访问 `/internal/v1/*`、`/worker/v1/*`、Driver 内部接口、源码仓库或主机 SSH。此检查发现违规行为；它不是完整网络审计或多租户安全证明。若需证明所有请求均符合路径白名单，应由操作员额外采集出口代理/服务端审计日志。
7. 检查创建与清理台账。取消/超时后继续按公开接口核对远端状态；删除测试容器不会取消远端 Run，也不会释放 VM。不可删除的发布记录和结果内容作为本轮命名的证据保留，不能声称“全部删除”。
8. 对结果目录、JSONL、stderr 和代码进行凭据扫描，包含本地已知 token 的明文、Base64、十六进制（含空白换行）匹配；通用规则和明文零命中不足以证明安全。发现实际凭据输出时中止该会话、限制原始证据访问，单独记录受影响凭据及失效处理，不能把删除容器等同于凭据已失效。原始资料默认私有，只有脱敏后的报告进入版本库；不要将完整日志或复制的认证文件提交。
9. 核对本轮容器和网络均已删除；证据收集完成后，删除操作员为本轮复制的认证文件，保留原有账户配置。再次执行需要重新通过安全渠道准备凭据。

浏览器认证必须由人通过统一认证入口完成；标准无交互容器不会自动完成扫码。需要有人在场的用例应单独安排会话并记录 intervention，不能因任务退出而声称恢复已通过。不得向被测 Codex补入业务实现或修复方案；如确需提示，应将该轮标记为 assisted，另开干净会话复测。

统一登录助手可以运行在操作员有浏览器的本机，不必从测试机下载文件或在测试容器中运行。认证绑定资源实例，隔离 agent 只需自己的 Observer 访问凭据即可调用已认证实例；不复制服务 Cookie。T6 必须在真实 `awaiting_auth` 出现后再登录，保留等待和恢复事件。若提交时实例已经 ready，可以验证已认证调用，但不能据此认定等待恢复通过，也不能撤销已有生产登录来补造前提。旧登录助手会话的过期状态与实例当前认证状态应分别核对。

每轮修复都重新构建 ZIP 并记录摘要；只复跑受修改影响的用例时，应报告这是跨轮次覆盖，不能声称最终 ZIP 在全部用例中完成了一次统一运行。容器准备失败与 agent 业务失败分别记录，网络创建超时等隔离预检失败不能计作业务用例已执行。

## 结果判定与报告模板

| 状态 | 判定 |
| --- | --- |
| `passed` | 业务验收、隔离边界、独立证据和清理核对全部满足 |
| `failed` | 前提满足但任务结果不正确、执行失败、越界、泄露凭据或证据不支持结论 |
| `blocked_environment` | 测试机、模型入口、Observer、网络或运行工具不可用 |
| `blocked_auth` | 需要授权且尚未完成，不能算业务成功 |
| `blocked_capability` | 资源、运行时、公开接口或元数据不能满足任务；记录具体缺口 |
| `not_applicable` | 启动前已经确认本环境没有可选用例所需资源；不能事后用来改写失败 |

基础可用性至少要求 T1、T2 通过；服务使用能力另要求选定 T4 通过，多节点能力另要求 T3 通过。认证恢复、故障清理、新包部署和真实 VM 生命周期分别给结论，不从基础通过外推。

报告至少记录：

```text
轮次 / 日期 / 测试宿主机：
skill ZIP SHA256 / 源码提交 / SDK / Codex / 模型 / 镜像 ID：
Observer 公开入口 / authority / workspace / 已知服务版本：
隔离预检结论与证据位置：
用例 / 前提 / 预算 / agent 状态 / 独立判定：
资源 URI / action URI / request_id / Run / 内容引用：
实际输出 / 独立预期 / 差异：
人工介入 / 重试 / 修复后新轮次：
创建对象 / 清理动作 / 清理确认 / 有意保留的证据：
凭据检查 / 剩余 blocker / 未覆盖能力：
```

出现阻塞时，保留真实失败阶段，先完成不依赖该条件的准备与文档。连测试机 SSH 握手都未完成时，结论只能是环境阻塞，不能记录“外部 Codex 已测试”。

实测示例见 [2026-10-27 验证报告](2026-10-27-skill-only-live.md)：包含 agent 自报通过但独立复核失败、公开契约阻塞、真实服务调用通过三种结果。
后续 [修复与完整用例复测](2026-10-27-skill-only-rerun.md) 保留各次失败及新轮次证据，并单独判定已认证调用、等待登录后恢复和包部署。
最新 [同一 skill 六任务复测](2026-10-27-skill-only-expanded.md) 使用一份冻结发布物覆盖多个业务目标，同时检查结果正确性、预算、报告一致性和凭据输出边界。

实现依据：[项目 skill](../../skills/personal-computing-space/SKILL.md)、[SDK 分发与用法](../../skills/personal-computing-space/references/sdk.md)、[服务 binding](../grip-service-bindings.md)、[能力包预检验证](../superpowers/reports/2026-10-26-package-deployment-preflight-validation.md)。Codex 的 skill 搜索位置与配置参考 [官方 skill 文档](https://learn.chatgpt.com/docs/build-skills) 和 [配置参考](https://learn.chatgpt.com/docs/config-file/config-reference)；运行时以锁定版本的实际目录检查为准。
