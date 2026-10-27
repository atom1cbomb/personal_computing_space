"""Execute the skill's public authoring example against a real embedded runtime."""
from pathlib import Path
import re

from loom_orchestration import ResourceRef
from tests.api.test_grip import setup, wait_result


async def test_skill_example_publishes_executes_and_returns_readable_remote_content(setup):
    app, repo, client, _ = setup
    reference = Path(__file__).resolve().parents[2] / "skills/personal-computing-space/references/function-packages.md"
    code, = re.findall(r"```python\n(.*?)```", reference.read_text(), re.S)
    namespace = {}
    exec(compile(code, str(reference), "exec"), namespace)
    schema = {"type": "object", "properties": {"value": {"type": "integer"}}, "required": ["value"]}
    published = await namespace["publish_function"](
        client, package_id="skill-double", package_version="v1", action_name="double",
        source='import json,sys; d=json.load(sys.stdin); print(json.dumps({"value":d["value"]*2}))',
        input_schema=schema, output_schema=schema,
    )
    description = await client.describe(published["resource"])
    action, = description["resource"]["metadata"]["actions"]
    assert action["mode"] == "request-reply"
    assert action["interaction"] == "task"
    assert action["output_schema"] == client.names.schema("run-output")
    slave = next(r for r in await client.resources(mcue="computing/slave") if r["metadata"]["available"])
    started = await client.invoke(slave["uri"], client.names.action("run-package"), {
        "package": published["resource"], "action": action["uri"],
        "arguments": {"value": 23}, "idempotency_key": "skill-double-first",
    })
    result = await wait_result(app, client, started["run"])
    assert result["state"] == "completed", result
    assert result["outcome"]["value"] == {"value": 46}
    assert await client.read_json(ResourceRef.model_validate(result["outcome"]["resource_ref"])) == {"value": 46}
