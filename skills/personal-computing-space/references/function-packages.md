# Python function packages

Use `package_type=function`, `execution={kind: process:json_stdio, version: "1"}`
for a single Python program on a Slave that advertises this executor. It reads
one JSON value from stdin and writes exactly one JSON value to stdout; diagnostics
belong on stderr. It runs with the Slave's Python standard library, without the
external client's SDK, files or credentials. Dependencies need a different
declared executor/package; do not assume the client virtualenv exists remotely.

`base_actions=[run_code]` is executor support, not a business action URI to invoke.
Select the exact Slave `run-package` offer and the published package's action URI.
Some older deployments advertise an executor `descriptor_ref` that cannot be
DESCRIBEd. Record that discovery defect; the versioned public authoring contract
below can still be used when the Slave advertises the matching executor and
publication validates it. Do not invent another executor URI or use an internal API.

## Publish through the public SDK

The function below publishes user-authored source and schemas; it performs no
execution. Pass an authorized unique package ID/version and an action name.
Use real schemas for your task. All content refs and digests come from Observer.
Do not include scope, publication state, Run provenance or package digest in the
manifest; the publication endpoint owns those fields.

```python
async def publish_function(client, *, package_id, package_version, action_name,
                           source, input_schema, output_schema):
    def wire(ref):
        return ref.model_dump(mode="json")

    program = await client.upload_content(source, media_type="text/x-python")
    inputs = await client.upload_content(input_schema, media_type="application/schema+json")
    outputs = await client.upload_content(output_schema, media_type="application/schema+json")
    io_contract = await client.upload_content({
        "schema_version": "io.v1",
        "input_schema_ref": wire(inputs), "output_schema_ref": wire(outputs),
    }, media_type="application/vnd.loom.io-contract+json")
    descriptor = await client.upload_content({
        "schema_version": "action.v1",
        "resource_id": client.names.name("actions", package_id, action_name),
        "name": action_name,
    }, media_type="application/vnd.loom.action-descriptor+json")
    return await client.register_package({
        "package_id": package_id, "package_version": package_version,
        "package_type": "function",
        "execution": {"kind": "process:json_stdio", "version": "1"},
        "action_exports": [{
            "action_descriptor_ref": wire(descriptor),
            "io_contract_ref": wire(io_contract),
            "effect_class": "Sandboxed", "permissions": [],
            "replay_safety": "DeclaredByPackage", "runtime_binding": {},
        }],
        "body": {"program_content_ref": wire(program)},
    })
```

For example, an integer doubling program can use
`import json,sys; d=json.load(sys.stdin); print(json.dumps({"value":d["value"]*2}))`
with the same input/output schema:
`{"type":"object","properties":{"value":{"type":"integer"}},"required":["value"]}`.
Adapt source and schemas to the user's task; do not replace remote execution with
a local answer. The `io.v1` supported schema subset is listed in [sdk.md](sdk.md).

## Execute and verify

1. DESCRIBE `published["resource"]`. Its `metadata.actions` gives the exact exported
   action URI, descriptor and IoContract refs. Preserve those refs for composition.
2. DESCRIBE an available Slave advertising `function / process:json_stdio / 1`.
   Read its `run-package` offer's input schema. Invoke that offer with the published
   package URI, discovered action URI, business arguments and a case-unique
   idempotency key. It returns `{run: URI}`; no service activation is necessary.
3. Poll that Run through its discovered `read` offer with a bounded deadline.
   Confirm completed state, remote attempt target and the persisted outcome.
   Fetch `outcome.resource_ref` using `client.read_json(ResourceRef.model_validate(...))`.
   A queued response or local oracle is not a completed remote computation.

For several dependent or concurrent nodes, publish the functions first and use
the `OrchestratorPackage`/`SubmissionBuilder` workflow in
[observer-workflow.md](observer-workflow.md). Each function has exactly one export;
the Python orchestrator is a separate executor with `async orchestrate(ctx, input_ref)`.
Return a completed node's result reference, including a final remote merge node
when needed for provenance. Record immutable published packages/content and any
Run-managed activation left as evidence; do not claim publication was undone.

For dependent nodes, the JSON input shape is part of the public contract:
`input_refs=[ref]` passes the referenced JSON value unchanged, while two refs
containing `{"value": 4}` and `{"value": 9}` become
`{"inputs": [{"value": 4}, {"value": 9}]}`. A merge function must declare and
consume an object with an `inputs` array; reusing a scalar worker expecting
`value` fails input validation before the merge executes. Publish a distinct
merge package when it has a different contract and preserve its own descriptor.
Await upstream results and pass their result refs directly to the merge node
so the persisted execution records its dependencies. Check the emitted node
inputs against their schemas before submitting; local contract checks do not
replace the required remote execution evidence.
