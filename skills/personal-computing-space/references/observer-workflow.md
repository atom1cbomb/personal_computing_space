# Observer External-Agent Workflow

The external agent is the planner. Driver accepts only an immutable execution
request and never receives prompts, messages, or source uploads.

## Discovery

1. Check `GET /healthz` and `GET /api/v1/runtime`.
2. Discover Slaves with `GET /api/v1/slaves?available_only=true`.
3. Discover packages, descriptors, schemas, and resource effects through the
   public API. For an existing resource, call
   `GET /api/v1/resources/{resource_id}/activations`; only an activation whose
   `target_resource_ref` exactly matches is reusable.

## Build and Submit

Use `loom-orchestration`:

```python
program_ref = await client.upload_content(program_source, media_type="text/x-python")
manifest = OrchestratorPackage(
    program_content_ref=program_ref,
    allowed_node_package_refs=node_packages,
    capability_descriptor_ref=orchestrator_descriptor,
    io_contract_ref=orchestrator_io_contract,
).manifest()
orchestrator = await client.register_package(manifest)
plan = SubmissionBuilder(
    goal="create and use a VM",
    body=TaskClosure(closure_id="vm-task"),
    orchestrator_package=ResourceRef(
        resource_id=f"capability-package://{orchestrator['package_id']}/{orchestrator['package_version']}",
        version_or_digest=orchestrator["package_digest"],
    ),
).build(request_id="request-opaque")
queued = await client.submit(request_id="request-opaque", plan=plan)
```

`202` means queued. Retry an uncertain response with the exact same
`request_id` and canonical body. A changed body returns
`409 request_id_reused`.

## Poll and Authenticate

Poll `GET /api/v1/run-submissions/{request_id}` or `GET /api/v1/runs/{run_id}`.
The relevant states are:

- `queued`, `committed`, `running`: keep polling;
- `awaiting_auth`: read `waiting`, call the exact package activation's
  `POST /api/v1/capability-packages/{package_ref}/auth/start`, and continue
  polling the same Run;
- `completed`: verify outcome, content refs, dynamic nodes, and evidence;
- `awaiting_decision`: present evidence and explicitly resolve or resubmit;
- `failed`/`cancelled`: report the structured outcome.

Authentication payloads are transient. Never put passwords, authorization
codes, cookies, private keys, or session tokens in a closure, source upload,
Run event, or ContentStore object.

## Repair and Cancel

Repair uses one complete replacement `ClosureContract` with
`base_closure_version` at `POST /api/v1/runs/{run_id}/resubmit`. Cancel only on
explicit user request with `POST /api/v1/runs/{run_id}/cancel`.

Use only the documented public Observer data-plane and submission paths; do
not call internal control-plane or worker endpoints.
