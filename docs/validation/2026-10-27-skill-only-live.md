# 仅凭 skill 的外部 Codex：2026-10-27 实测报告

**指定测试机已能通过 `root@9.0.3.9:22` 登录。隔离预检通过，三个全新 Codex 会话完成了 T1、T2、T4。现有 Volcano 服务调用通过；资源发现报告存在契约解释错误，通用远端计算被公开契约发现问题阻塞，因此基础可用性验收尚未通过。**

本轮按 [通用验证流程](skill-only-codex.md) 执行。root 只用于操作员准备和隔离配置；被测进程没有 Observer、Slave 或其他资源所在机器的 SSH 凭据与权限。早期连接超时保留在 [准备与阻塞记录](2026-10-27-skill-only-preparation.md)，不再作为当前结论。

## 固定输入与隔离

| 项目 | 本轮值 |
| --- | --- |
| 测试宿主机 | `9.0.3.9`，操作员使用 root，agent 使用容器 UID 1000 |
| 源码基线 | `e447c3b`；验证运行器、镜像定义和文档为本次新增内容 |
| 唯一启用 skill | 发布版 `personal-computing-space`，附独立 `loom-orchestration` SDK 0.8.0 |
| Codex | CLI `0.164.0`；三个业务会话均使用 `gpt-5.6-terra` |
| 模型连接 | 测试机原有、已授权的 Responses 服务；仅提取模型连接字段，不继承宿主机配置、插件或历史 |
| Observer | `http://9.0.16.66:8080`，可信测试网络 |
| GRIP | authority `loom.local`，workspace `workspace-default` |
| 生产服务版本 | 未独立取得部署提交号；不以开发机 HEAD 代替 |
| 介入 | 0 次业务提示、0 次人工代写代码；没有向被测会话补入修复方案 |

```text
skill ZIP SHA256:
e270bf4f8499cf13a4d02cad218c55256d3af4f2e3536fb12fe5bf79af2b83c0

测试机实际使用的镜像 ID:
sha256:9f4ae20c9dbbc5913c16343fc316766ab50667130b805e03d3de2b9d09801c77
```

完整预检在北京时间 14:20:13～14:20:22 通过，每个业务用例另行执行相同预检：非 root、空工作区、无服务端模块、无 SSH 客户端/agent/用户密钥目录、无 sudo、无 Docker socket。实际 `skills/list` 确认仅项目 skill 启用；CLI 自带的系统 skill 全部禁用。

每例使用独立 bridge 网络，容器网络命名空间内同时阻断 IPv4/IPv6 TCP 22 出站。根文件系统只读，移除全部 capability，启用 `no-new-privileges`，不挂载源码或宿主机 home。home 和临时目录为临时文件系统；模型和 GRIP 凭据私有注入。镜像构建曾因测试机没有默认 bridge 而失败，操作员改用构建期 host 网络；被测会话始终使用独立 bridge。

## 用例结果

时间均为北京时间，耗时包含容器准备与清理。独立判定使用另一客户端读取公开 Observer/GRIP 接口，原 agent 报告保持不变。

| 用例 | 时间 / 耗时 | 预算 | agent 自报 | 独立判定 |
| --- | --- | --- | --- | --- |
| T1：资源与契约发现 | 14:21:02～14:27:07 / 365 秒 | 10 分钟，只读，不创建 Run | `passed` | `failed`：资源发现正确，契约解释错误 |
| T2：远端整数统计 | 14:28:43～14:38:26 / 583 秒 | 15 分钟；最多 1 Run、4 节点、2 并发，执行 300 秒 | `blocked` | `blocked_capability`：未完成普通函数包的编写与提交 |
| T4：Volcano 只读业务查询 | 14:39:29～14:44:38 / 309 秒 | 10 分钟；最多检查 3 实例、6 次业务查询，每 Run 等待 120 秒 | `passed` | `passed`：两个真实 Run 完成，内容引用及摘要独立校验通过 |

### T1：发现成功，运行语义解释未通过

agent 发现 2 个在线 Slave、28 个能力包、48 个激活实例，以及 2 个 VM、3 个 Volcano、3 个 Grafana 和 1 个 service 资源。能够区分发布、在线、ready、pending_auth 和 degraded 等状态，没有创建业务 Run。

但报告把 GRIP `request-reply` 推广为“服务业务调用不产生 Run”。独立 DESCRIBE 显示，Volcano `namespaces-list` offer 的 `mode` 虽然为 `request-reply`，其 `output_schema` 却是 `schemas/run-output/v1`；对应运行绑定采用 task 语义。T4 随后实际产生的 Run 也证实了这一点。传输协议与业务执行类型不能混为一谈，故 T1 整体不记通过。

### T2：尚未证明可完成通用远端计算

任务要求在远端统计 1～10000，期望为 `count=10000`、`sum=50005000`、`sum_squares=333383335000`、`min=1`、`max=10000`。允许自行上传程序/契约并发布带本轮前缀的包。

agent 最终给出 `function_package_authoring_contract_unavailable`：没有找到既有的普通函数包，也未从 skill 和公开接口取得足够的包编写契约，因此没有发布包、上传任务内容或创建业务 Run。它仅生成本地验算值，并明确没有远端执行结果。

独立检查发现两个 Slave 均广告了 `run_code` 和 `function / process:json_stdio / 1` 执行器，但其提供的描述 URI：

```text
grip://loom.local/workspaces/workspace-default/executors/function/process%3Ajson_stdio/1
```

通过 GRIP DESCRIBE 返回 `not_found`。当前 skill/SDK 中有服务包、编排包支持及部分 stdio 规则，但本次 agent 未能据此完成普通函数包。结论限于“无辅助的函数编写、提交链路未完成，且执行器公开元数据存在缺口”；不能推导为没有算力节点或执行器不可运行。

### T4：真实业务执行通过

agent 发现三个 Volcano 实例，选择北京 ready 实例，读取动作参数契约后查询队列与命名空间。三个资源报告的 `kubernetes_ref` 均与独立 DESCRIBE 一致。

选中实例为 `activations/volcano-http/v2/slave-a/beijing`，revision 为 1；下列 URI 均位于 `grip://loom.local/workspaces/workspace-default/` 下。

| 动作 | Run | 实际目标 | 返回结果 | 结果内容 SHA256 |
| --- | --- | --- | --- | --- |
| `queues-list`，输入 `{}` | `run-b6742792377f` | `slave-a` | `status=ok`、`complete=true`；2 个队列 `default`、`root`，均为 `Open` | `18881f7470923cc07ec4b881ff543b7feb0f9d470f16485f711404e387d1c2df` |
| `namespaces-list`，输入 `{}` | `run-9ed9017b7a75` | `slave-a` | `status=ok`、`complete=true`；51 个命名空间 | `d1aec29013a2fdeb6b34019195fc2c18a9c19c353d3b96afa55f2112dc9309d5` |

操作员从公开 Run 接口确认 completed 状态、实际 attempt、`container:http` 执行来源及 `volcano-http/v2` 包引用，再按 Observer 返回的完整 `ResourceRef` 取回内容。内容与 Run outcome、agent 留存响应一致；按 RFC 8785 规范化后计算 SHA256，与引用中的内容摘要一致。Slave 和 Observer 的 schema validation evidence 均为 pass。

原脚本使用了固定幂等键 `volcano-read-queues_list` 和 `volcano-read-namespaces_list`。独立核对执行事件在本例时间范围内，因此本次确为新执行；后续轮次应使用唯一前缀，避免把旧 Run 误算为新验证。此要求已补入通用流程，没有追改本次提示词或 agent 证据。

两个完成的 Run 和结果内容作为证据保留。agent 报告中的“无创建对象”只适用于业务对象；不能理解为没有留下 Run 记录。本轮未创建或修改队列、PodGroup、Job、VM 或激活实例。

## 证据、密钥检查与清理

私有完整证据保存在测试机 `/root/loom-skill-validation/evidence/`，并归档到开发机被 Git 忽略的 `production/skill-validation/20261027/`。每例保留完整提示词、运行摘要、预检、JSONL、stderr、生成代码和原报告。独立复核保存在 `t1-independent-review.json`、`t2-independent-review.json`、`t4-independent-review.json`，另有公开目录、执行器描述、Run 与结果内容证据。

以下提示词摘要用于确认冻结输入；重跑时应保留新一轮的完整提示词与独立证据：

| 用例 | prompt.txt SHA256 |
| --- | --- |
| T1 | `9891e519d48ac43202ed88e0f6a3e4f75a4eb9580e9dbd63ab402a978b0647b1` |
| T2 | `bb095ec5e250ef3bb6fdcfaf5fa600c352977e284ca814c268b98eecf2f136bd` |
| T4 | `903d2b2f9dcd45c5d35294f696ee86da72c271fc0fc0ccacfe3ece72e951990a` |

三例分别检查了 15、16、17 个证据文件，包含完整日志、生成代码与报告，不包含安装的依赖。对本轮真实 GRIP token 和模型 API key 的精确匹配结果均为零。审阅执行命令和生成代码，未发现资源主机 SSH、内部控制接口调用、源码下载或读取其他 skill；这不是完整网络审计证明。

本次提交的 7 个文件另经 Gitleaks 8.30.1 及本地已知凭据精确匹配扫描，未发现密钥。私有原始证据和发布 ZIP 均被 Git 忽略，不在提交清单内。

测试机上本轮容器、独立网络均已删除；为本轮复制的 `private/grip-token` 和 `private/codex-provider.json` 已删除，原有账户凭据保持不变。结果与 Run 留存不影响其他业务资源。原始日志、认证和业务原始数据不进入 Git。

运行器的 11 项针对性测试通过，覆盖发布包边界、目录穿越、符号链接、服务端源码混入、宿主网络保护，以及最小模型配置和凭据隔离。

## 后续范围

基础验收需要 T1、T2 均通过，本轮未满足。T4 证明该模型在这份 skill 和当前部署下能够无辅助使用既有 Volcano 能力完成真实只读任务，不代表其他服务或任意计算任务都可用。

下一轮应先修复执行器描述 URI 的公开可读性，补齐普通函数包的发布契约或最小 skill 示例，再用原计算目标启动干净会话。另需明确 GRIP mode、运行 interaction 和输出 schema 的关系，验证 agent 能正确解释。修复后另开轮次，不覆盖本轮失败记录。

T3 多节点、T5 幂等与取消、T6 认证恢复、T7 新包部署和 VM 生命周期本轮未执行，均不作通过声明。
