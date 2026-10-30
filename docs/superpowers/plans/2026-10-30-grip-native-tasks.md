# GRIP Driver 与原生任务 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** 完成 issue #2，让调用方通过 GRIP 发现单 Driver、提交完整计划、读取冻结计划并管理 Run 生命周期。

**Architecture:** Observer 把现有 Driver 注册、ExecutionRequest 和 Run 投影为资源；提交、修复、取消和恢复继续使用原有 repository 事务和状态机。冻结计划 URI 由 Run 与 closure version 构成，正文直接取执行请求保存的 closure_contract；不增加数据库表、调度器或多 Driver 逻辑。删除被替代的任务 REST 接口，SDK、Web 与测试使用同一 GRIP 契约。

**Tech Stack:** Python、FastAPI、Pydantic、SQLAlchemy、现有 GRIP、pytest/httpx、原生 JavaScript。

遵循 [设计原则](../spec-design-guidance.md)。范围来自 [issue #2](https://github.com/yzs15/personal_computing_space/issues/2)；保持单 Driver，自动路由留在 #3。当前 main 基线 `479846f`，worktree `.worktrees/grip-native-tasks`，分支 `feat/grip-native-tasks`。

## 确定的公共契约

| 资源 | URI（相对 workspace） | MCUE | 动作 |
| --- | --- | --- | --- |
| 逻辑 Driver | `drivers/default` | `computing/driver` | `submit-plan`、`get-submission`、`how-to-use` |
| 冻结计划 | `plans/{run_id}/{closure_version}` | `execution/plan` | `read`、`how-to-use` |
| Run | `runs/{run_id}` | `execution/run` | `read`、`cancel`、`resubmit`、`resolve`、`close`、`how-to-use` |

- Driver 永远可发现；`accepting_submissions=true` 表示 Observer 可接收，`available` 表示有效 Driver 租约。instance/epoch 属于运行 metadata，重启不改变 URI。
- `submit-plan` 输入为 `{plan: ClosureContract, idempotency_key: string, conversation_ref?: string}`；业务幂等键映射现有 repository `request_id`，与 GRIP 消息 `request_id` 独立。接受后输出 `{run: URI}`；readiness 拒绝通过动作输出的 `{rejected: DomainErrorEnvelope}` 分支返回具体 blockers，保持 GRIP v1 核心信封不变。
- `get-submission` 输入 `{idempotency_key}`，返回已有提交投影并补充 `run`、`plan` URI。因此通信超时后无需重新构造计划即可找回任务。所有查询按当前 workspace 隔离。
- `resubmit` 输入 `{plan, idempotency_key, base_closure_version}`，接受后返回 `{run}`，readiness 拒绝同 submit-plan。保留成功条件、预算、恢复策略、版本检查和原有修复限制。
- `cancel` 输入 `{reason?: string|object, idempotency_key?: string}`；提供提交键时核对其所属 Run。`resolve` 输入 `{decision: 'accept'|'abandon'}`，`close` 输入 `{}`。返回 Run 状态投影。
- Run `read` 默认返回轻量状态、outcome、Run URI/id、当前冻结计划 URI/version/digest 和 execution id/epoch；输入可选 `include_history=true` 时返回现有完整 Run 视图及计划引用。目录和默认读取不加载 events/draft/整段计划。
- 冻结计划 `read` 返回 `{run, closure_version, snapshot_digest, plan: ClosureContract}`。`plan` 正文的 body 与已提交 snapshot 一致；目录只读摘要字段。修复后旧 URI 仍读回旧正文。
- 所有生命周期 action 保持可发现，说明中写明合法状态；服务端执行原有状态校验。这样重复请求可命中原有幂等处理，不因状态改变而在发现层被提前拒绝。
- 原生任务错误公开稳定代码，如 `request_id_reused`、`version_conflict`、`readiness_blocked`、`run_not_terminal`、`invalid_decision`；不返回内部异常或凭据。

## 文件职责

- `loom_v2/observer/repository.py`：轻量 Run/计划投影、冻结正文读取、现有事务。
- `loom_v2/observer/native_tasks.py`（新）：GRIP 原生任务 Pydantic 契约、URI/视图及动作处理。
- `loom_v2/observer/grip.py`：把原生资源接入现有目录、schema、describe/invoke 和使用说明。
- `loom_v2/mcue_types/computing-driver-v1.json`、`execution-plan-v1.json`（新）：公开类型定义。
- `loom_orchestration/client.py`：SDK helper 改用 describe/invoke，沿用现有 Python 参数便于调用方表达业务幂等键，消除任务 REST 依赖。
- `loom_v2/observer/app.py`、`loom_v2/contracts/api.py`：删除被替代的任务 HTTP 路由及仅服务它们的模型。
- `loom_v2/web/static/*`：使用用户输入的 GRIP token 与 workspace URI 发现和调用，不在页面内嵌服务端 secret。
- `tests/api/test_grip_native_tasks.py`、`tests/api/test_native_plan_projection.py`（新）及既有 SDK/Driver/API/UI 测试：覆盖新入口和执行语义。
- `README.md`、`docs/grip-resource-usage.md`、`examples/grip/*`、`skills/personal-computing-space/*`、相关 scripts：更新现行使用方法，历史 spec 只增加指向新设计/计划的说明。

## Task 1：冻结计划和轻量 Run 投影

- [x] 先增加持久化投影测试：SQLite Observer A 写入后 B 能读取；目录不加载 `_load`/events/draft；外 workspace 不可见；修复后旧计划正文不变。
- [x] 为 `RunSummary` 增加可空 `closure_version`、`snapshot_digest`、`execution_id` 和 `execution_epoch`，SQL 使用 JSON 字段投影读取 committed version/digest。
- [x] 新增冻结计划摘要与读取接口（不新增持久字段）：

```python
@dataclass
class PlanSummary:
    run_id: str
    closure_version: str
    snapshot_digest: str

```

  `list_plan_summaries(workspace_id, *, run_id=None, closure_version=None)` 返回 `list[PlanSummary]`；
  `read_frozen_plan(workspace_id, run_id, closure_version)` 返回 `dict[str, Any]`。
  SQL 从 `RunExecutionRequestRow` 选择所需列，内存分支从 `execution_requests` 投影；正文返回 closure_contract，校验 Run 属于 workspace。提交与修复保存 `record.closure_contract.model_copy(update={"body": committed.snapshot}).model_dump(mode="json")`。
- [x] `read_run_result` 继续轻量查询，附加上述 Run 标识和版本字段；完整历史只按显式请求读取。只有确有冻结快照的版本才发布 plan URI；SDK/Web 选取精确的 drivers/default，避免与普通登记资源的同 MCUE 混淆。
- [x] 执行：`/root/loom_v2/.venv/bin/python -m pytest tests/api/test_native_plan_projection.py tests/api/test_grip_directory.py -q`，更新受字段扩展影响的精确字典断言并确认通过。

## Task 2：GRIP 原生资源和动作

- [x] 新测试先验证 Driver 离线可发现/排队、schema 可读取、未知 workspace 拒绝、同键同参复用/异参冲突、关闭非终态拒绝。
- [x] 在 `native_tasks.py` 定义输入模型，直接引用已有 `ClosureContract`：

```python
class SubmitPlanInput(Model):
    plan: ClosureContract
    idempotency_key: str = Field(min_length=1, max_length=256)
    conversation_ref: str | None = Field(default=None, min_length=1, max_length=256)

class SubmissionLookupInput(Model):
    idempotency_key: str = Field(min_length=1, max_length=256)

class ResubmitPlanInput(Model):
    plan: ClosureContract
    idempotency_key: str = Field(min_length=1, max_length=256)
    base_closure_version: str = Field(min_length=1, max_length=128)

class RunReadInput(Model):
    include_history: bool = False

class ResolveRunInput(Model):
    decision: Literal['accept', 'abandon']
```

- [x] 注册上述 action 的 schema/output 与 MCUE；Driver 发布 `ClosureContract` schema 引用、格式版本和 `container:python_orchestrator/1` 支持信息。其 binding 继续为 Observer。
- [x] 资源目录通过 source/type/target 过滤只读相关数据源；Run metadata 关联 `plan`、逻辑 `driver` 和执行版本。
- [x] invoke 调用 `submit_run_plan(..., source='external')`、`resubmit_run_plan`、`cancel_run`、`resolve_run`、`close_run`，查询提交复用现有字段及认证等待投影。用现有 execution transaction 包含需要“核对后写入”的生命周期操作。
- [x] 在 `how-to-use` 解释单 Driver、离线排队、计划/运行关系、幂等与各动作状态限制；action schema 和错误保持可读可解析。
- [x] 执行：`/root/loom_v2/.venv/bin/python -m pytest tests/api/test_grip_native_tasks.py tests/api/test_grip.py tests/api/test_grip_directory.py -q`。

## Task 3：SDK、Web 和旧入口迁移

- [x] SDK 的 `submit(request_id=..., plan=...)` 调用 Driver `submit-plan` 后用 `get-submission` 取得提交详情；`submission` 直接 lookup；`run` 调用 Run `read(include_history=true)`。
- [x] `resubmit` 调用对应 action 后 lookup；`cancel` 将可选 request_id 转换为业务提交键；`resolve/close` 调用对应 action。使用现有 Names、invoke、schema 校验和鉴权，不增加 HTTP 重试。
- [x] Web 增加 token（password，仅保留内存）和 workspace URI 输入；GRIP 信封包含独立消息 ID；通过 workspace list-resources 查 Driver，然后从 DESCRIBE offers 选择 action 调用。Run 输入支持返回的 URI。
- [x] 删除 `/api/v1/run-submissions` GET/POST 和 Run cancel/resubmit/resolve/close POST 路由、仅用于这些路由的请求模型与 submission view。完整 Run GET 由 GRIP `read(include_history=true)` 替代，同步迁移调用点。
- [x] API 测试使用 `tests/support/grip_client.py` 的真实 GRIP 请求；对旧路由增加 404/405 验证。SDK 测试确认所有任务操作实际经过 GRIP、消息鉴权和 schema。
- [x] 执行：`/root/loom_v2/.venv/bin/python -m pytest tests/contracts/test_orchestration_client.py tests/contracts/test_grip_examples.py tests/web tests/api/test_run_submission.py tests/api/test_observer_run.py tests/api/test_readiness.py tests/api/test_runtime.py tests/api/test_conversation.py -q`。

## Task 4：执行与恢复验收、使用说明

- [x] 真实 process 执行普通任务；编排任务沿用已有仅替换容器进程的测试，验证动态节点仍通过 Observer/Slave。
- [x] 验证 Driver 注册/重启后 URI 稳定，旧 epoch 不能回报；离线排队后恢复领取同一 Run，未产生重复执行。
- [x] 验证人工 attestation accept、repair resubmit、版本冲突、成功条件保护、取消与关闭，修复前后计划均可读；追加拒绝跨 workspace 的测试。
- [x] 更新 README、GRIP 指南、SDK 示例、通用 skill 与当前 scripts，历史设计文档标注接口已被本计划替代。SDK 版本提升到 `0.10.0`，说明任务调用需要 GRIP token。
- [x] 对本改动做 spec 审查和代码质量审查，修复实际发现的问题。

## Task 5：最终验证与交付

- [x] 执行相关 API/契约/Driver/Slave/UI 测试；再跑无需部署的完整测试集：`/root/loom_v2/.venv/bin/python -m pytest tests -m 'not integration and not e2e' -q`。
- [x] 执行项目既有静态检查（若配置）、`git diff --check`，构建独立 SDK/skill 包的现有测试，确认 SDK 不依赖服务端模块。
- [x] 在 `docs/superpowers/reports/2026-10-30-grip-native-tasks-validation.md` 记录实际命令、结果、未运行的外部部署测试和影响范围。
- [x] 将通过验证的代码和计划提交到当前功能分支，保留 worktree 供审阅；不自动部署或合并 main。

## 基线

`tests/api/test_grip.py tests/api/test_grip_directory.py tests/api/test_run_submission.py tests/contracts/test_orchestration_client.py tests/driver/test_worker_discovery.py`：30 passed（23.70s）。

## 执行结果

全部实施步骤完成，spec 与代码质量复核通过。完整回归的 23 项 Docker 环境基线失败已在未修改 main 逐项复现；本次未新增失败。具体命令、计数和浏览器检查见 [验证报告](../reports/2026-10-30-grip-native-tasks-validation.md)。
