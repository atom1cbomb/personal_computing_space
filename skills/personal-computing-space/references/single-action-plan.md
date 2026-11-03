# Single action as a task

Use this template when one ordinary JSON action needs Run records and recovery.
Discover the activation and action, read their business schemas, and check
`execution.task.available`. Inspect the logical Driver for submission readiness.
The plan pins the package digest and activation revision. It does not provision
another instance. Choose budgets and replay safety for the business operation.

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

Read [observer-workflow.md](observer-workflow.md) for Run polling, repair and cancellation.
