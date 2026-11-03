# Action 由调用方选择直接执行或任务执行

状态：已实现并完成生产部署与 E2E 验收，见[生产报告](../reports/2026-11-02-action-execution-production.md)。以下描述本次变更及验收要求。

## 1. 问题与目标

当前 `runtime_binding.interaction=task` 会让业务 action 接收任务包装并返回 Run，且不能直接调用；`request-reply` action 已可直接调用或绑定到 Run。此次让普通业务 action 的执行方式统一由调用方选择。

同一份业务实现和输入输出契约提供两条路径：直接 `INVOKE` 返回业务结果；调用方将 action 放入计划，通过 `submit-plan` 提交并取得 Run。外部 agent 使用资源描述和单节点模板准备计划。

## 2. 交互声明与业务入口

`runtime_binding.interaction` 的取值改为 `request-reply`、`byte-stream`，默认值改为 `request-reply`。删除 `task` 值、业务 action 的任务包装 Schema 及按旧 interaction 选择提交 Run 的分支。

普通 JSON 对象输入输出的 `container:http` 与 `process:json_stdio` action 支持直接调用和任务节点执行。函数的直接入口按其声明的对象输出契约返回结果。字节流通过 `open_stream()` 使用；依赖 Run 上下文的 Python 编排程序通过完整计划执行。

原 task 业务 action 的 offer 改为 IoContract 的业务输入输出，Observer relay 和 Slave 直调入口按新的交互声明接受调用，并能为这些 action 签发服务授权。

## 3. Slave 执行与取消

将 Slave 的 export 调用及输入输出、成功语义校验提取为共享逻辑，供直接入口与任务入口使用。HTTP 服务调用 runtime plugin host；普通函数通过 executor registry 执行。

直接入口仅管理本次请求状态；任务入口包裹持久 Attempt 与结果落盘。两条入口的校验结果必须一致，直接执行不能产生任务账本记录。

补齐普通函数的直调取消处理：deadline 到期或调用协程取消时终止并回收本次进程组。HTTP 请求中断可能发生在上游已接受操作之后，调用方需要核对结果。

Slave 的 Compose 部署启用 `init: true`，由 init 回收进程组终止后失去父进程的子进程。

## 4. 直调期限

移除 Observer relay 与 Slave 入口在未传 deadline 时施加的 30 秒默认超时。服务端使用显式 deadline（若有）与当前 grant 到期时间中较早的期限。

relay、Slave 和 runtime plugin 逐层传递本次调用的剩余期限。SDK 的 HTTP 等待超时支持较长值及 `None`，与请求 deadline 分开配置；连接建立超时可单独配置。直接执行可以超过 30 秒，选择 task 的依据是记录、调度与恢复需求。

## 5. 执行可用性与预检

`resource.metadata.actions[]` 增加 `execution`：

```json
{
  "interaction": "request-reply",
  "execution": {
    "direct": {"available": true, "unavailable_reason": null},
    "task": {"available": true, "unavailable_reason": null}
  }
}
```

出现的键表示运行时与契约支持该执行方式；`available` 表示当前已知条件是否满足，失败时给出原因。

- direct 检查运行时、activation、授权签发与路由配置。
- task 检查动作、运行时与 activation 的节点执行条件。Pool 接收状态从 Driver 描述获取，完整计划由提交检查判定。
- action 的整体 `available` 改为至少一种执行方式可用。直调 offer 缺失时，metadata 中仍可发现可作为任务节点的 action。
- 未激活包的执行方式标为 `activation_required`。领域资源具有唯一就绪访问实例时投影该实例的 action 声明。

`check-package.ready` 改为运行时与配置是否满足激活条件，各执行方式的问题单独报告。缺少公开直调 binding 不应阻断可执行 task 的包激活；更新 `ServiceRoutes.require()` 及相关可用性计算。

## 6. SDK 与 agent 指南

`inventory()` 与 `action_contract()` 输出执行可用性。`execute_action()` 删除按 interaction 自动提交 task 包装和轮询的分支，改为检查 direct 可用性并直接调用。任务由调用方准备计划后使用 `client.submit()` 提交。

项目 [personal-computing-space skill](../../../skills/personal-computing-space/SKILL.md#decide-how-an-action-executes) 集中加入以下指导：

- 探索、参数验证、短调用测试和实时查询优先直接执行；调用方处理未收到结果时的核对与容错。
- 需要中断后找回工作、保存结果或按节点恢复时，优先任务执行，并承担排队、调度、存储与查询成本。
- 两种方式都真实执行业务。直接调用超时后先核对原操作，不能自动切换为 task 重做。
- Run 的恢复粒度是显式节点，外部副作用幂等和 action 内部步骤的责任见[项目说明](../../../README.md#编排恢复与-action-的责任边界)。

参考文档加入发现、计划构造及恢复示例：从领域资源选择精确 activation，读取包、action、Slave、实例与 revision，按附录构造计划；提交前保存原计划和业务键，响应丢失时按原键查询。

同步修改 `references/service-bindings.md`、`references/reliable-workflows.md`、`references/sdk.md`、`references/existing-activations.md` 和 `references/observer-workflow.md` 中的旧 task 包装说明。包的 `HOW_TO_USE.md` 更新受影响的调用语义与业务注意事项。

## 7. 实现与发布范围

| 范围 | 改动 |
| --- | --- |
| 包契约与发布器 | interaction 枚举、默认值、生成器和 manifests；重发受影响包版本 |
| Observer | 业务 offer Schema、服务授权、执行可用性投影和激活预检 |
| Slave／runtime | 共享 export 执行与校验、普通函数直调、取消回收及期限传递 |
| SDK | 执行可用性、直接调用 helper、可配置 HTTP 等待超时 |
| skill／文档 | 通用选择指南、单节点模板、受影响的调用示例与包说明 |

升级需协调服务、SDK、包与 skill 版本。变更包契约和文档后发布新包版本并更新 activation；切换前完成或显式终结依赖旧契约的活动 Run。历史记录按原冻结内容读取，新目录筛除旧可执行契约。SDK wheel、skill 压缩包及下载 manifest 随匹配版本发布。

## 8. 验收

1. 原 task 与原 request-reply JSON action 均可直接执行或绑定为单节点计划；业务结果一致，直接执行不写 Run／Attempt／自动结果内容。
2. 发布校验拒绝 `interaction=task`，省略时按 request-reply 处理。业务 offer 使用业务 Schema。
3. 普通函数两条路径的输入输出与成功语义校验一致；直调取消、超时后没有遗留子进程。共享执行重构后的任务结果落盘与 Attempt 重放通过回归。
4. 在有效授权及相应客户端等待配置下，直连和 relay 的无显式 deadline 调用均能超过 30 秒；显式 deadline 与 grant 到期仍生效。
5. 缺少直调 binding 时可激活并作为任务执行；DESCRIBE 正确区分两种执行方式。恢复路由后直调恢复。
6. SDK 发现结果保留 execution 信息；直接调用 helper 对 direct 不可用的动作报错，发生承载失败时不改走任务提交。
7. agent 仅使用发布 skill、SDK 和资源描述，能够为探索试查选择 direct，为恢复需求准备计划并调用 submit-plan；单节点模板通过公开契约校验。
8. 包示例、SDK、下载 skill 与新语义一致；旧冻结记录可读取，新目录和指南不再引导旧 task 包装调用。

## 附录：加入 skill 参考文档的单节点模板

`activation_uri`、`action_uri`、`arguments` 和稳定业务键 `key` 由调用方选择。以下用于首次准备；重试加载已保存的计划，预算按业务需求设置。

```python
from loom_orchestration import ClosureContract, TaskClosure

metadata = (await client.describe(activation_uri))["resource"]["metadata"]
package = await client.invoke(metadata["package"], client.names.action("read"), {})
export = next(a for a in metadata["actions"] if a["uri"] == action_uri)
action_ref = export["action_descriptor_ref"]["resource_id"]
input_ref = await client.upload_content(arguments, name="single-action/input")

plan = ClosureContract(
    closure_id=key, goal="执行选定 action", workspace_id=client.workspace_id,
    allowed_effects=export["permissions"],
    resource_budget={"execution_timeout_seconds": 120},
    recovery_policy={"allow_reassignment": False},
    body=TaskClosure(
        closure_id=key,
        program={"action_ref": action_ref, "io_contract_ref": export["io_contract_ref"]},
        compute={"action_ref": action_ref, "typed_holes": [
            {"hole_id": "target", "status": "bound", "binding_ref": "binding"}
        ]},
        compute_bindings=[{
            "binding_id": "binding", "hole_id": "target", "bound_by": "agent",
            "action_descriptor_ref": export["action_descriptor_ref"],
            "capability_package_ref": {
                "resource_id": metadata["package"],
                "version_or_digest": package["package_digest"],
            },
            "target_resource_ref": {"resource_id": metadata["host"]},
            "instance_name": metadata["instance_name"],
            "expected_activation_revision": metadata["revision"],
        }],
        node_input_bindings=[{
            "node_id": action_ref, "input_ref": input_ref.model_dump(mode="json")
        }],
    ),
)
# 提交前保存 key 和 plan.model_dump(mode="json")，重试时原样加载。
submission = await client.submit(request_id=key, plan=plan)
state = await client.run(submission["run"])
```
