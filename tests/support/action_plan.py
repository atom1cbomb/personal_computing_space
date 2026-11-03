"""Exercise the documented single-node plan using only the public SDK."""

async def submit_activation_plan(client, activation_uri, action_uri, arguments, key):
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
    return submission
