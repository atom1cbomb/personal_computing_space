# Observer External-Agent Workflow

The external agent is the planner. Driver accepts only an immutable execution
request and never receives prompts, messages, or source uploads.

## Discovery

1. Check `GET /healthz` and `GET /api/v1/runtime`.
2. Use `client.resources(mcue="computing/slave")` and inspect descriptions for `run-package` /
   `activate-package` offers. Filter `metadata.available` and obtain the exact
   callable offers with `client.describe(uri)`. Use the operator's GRIP authority,
   workspace and token configuration (see [sdk.md](sdk.md)). Resource typing and
   MCUE filtering are described in [mcue.md](mcue.md).
3. Discover package and resource URIs through `client.resources`. Inspect action
   descriptions for verified descriptors and schema URIs. For an existing domain
   resource, DESCRIBE lists explicitly bound ready activations. Select the exact
   returned activation URI; do not infer bindings from package availability.

For a single task, invoke the Slave's `run-package` action with the package
URI, exported action URI, arguments and stable idempotency key. Its returned
`run` URI identifies a Run resource with `read` and `cancel` actions. If you need
to publish a new Python computation first, use [function-packages.md](function-packages.md).

Long-lived services use `activate-package`; invoke their ready activation with
the selected export's declared interaction and offered schemas. Task exports use
`{arguments, expected_revision, idempotency_key}` and return a Run; direct exports
take business input and return business output. GRIP `mode=request-reply` applies
to both and does not distinguish them. Stream exports use `open_stream`.
Select its credentials with top-level `credential_profile`, alongside
`instance_config`, using a name supplied by the Slave operator. Different named
instances can use different profiles. The selection is not in the package or
DESCRIBE; confirm the deployed input schema supports it (see [sdk.md](sdk.md)).

## Build and Submit

Install `loom-orchestration` using [sdk.md](sdk.md). The following fragment
assumes discovery has returned exact `node_packages`, and content upload has
returned the program input, descriptor and IoContract refs. The program's
action id must match its descriptor; no placeholder refs are accepted:

```python
program_ref = await client.upload_content(program_source, media_type="text/x-python")
manifest = OrchestratorPackage(
    program_content_ref=program_ref,
    allowed_node_package_refs=node_packages,
    action_descriptor_ref=orchestrator_descriptor,
    io_contract_ref=orchestrator_io_contract,
).manifest()
orchestrator = await client.register_package(manifest)
plan = SubmissionBuilder(
    goal="create and use a VM",
    body=TaskClosure(
        closure_id="vm-task",
        program={
            "action_ref": orchestrator_action_id,
            "io_contract_ref": orchestrator_io_contract.model_dump(mode="json"),
        },
    ),
    orchestrator_package=ResourceRef(
        resource_id=orchestrator["resource"],
        version_or_digest=orchestrator["package_digest"],
    ),
    input_binding=input_ref,
    allowed_effects=approved_effects,
).build(request_id="request-opaque")
queued = await client.submit(request_id="request-opaque", plan=plan)
```

`202` means queued. Retry an uncertain response with the exact same
`request_id` and canonical body. A changed body returns
`409 request_id_reused`.

## Poll and Authenticate

Poll `GET /api/v1/run-submissions/{request_id}` or `GET /api/v1/runs/{run_id}`.
The relevant states are:

- `queued`, `committed`, `running`, `cleaning_up`: keep polling;
- `awaiting_auth`: match `waiting`'s package digest, Slave, instance and revision
  to an activation returned by `client.resources(mcue="capability/activation")`. DESCRIBE it and
  invoke its `auth-start` offer with `expected_revision`. Start once and continue
  polling the same Run. Do not resubmit for authentication recovery;
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
Use `await client.cancel(run_id)`; if including `request_id`, it must be the
original submission's id, not a new cancellation id.

A GRIP `run-package`/task action's `idempotency_key` is **not** the
`request_id` of `client.submit`. For a Run returned by GRIP, omit `request_id`
when cancelling, or invoke the Run's discovered GRIP `cancel` offer with its
declared input. Passing a GRIP idempotency key as `request_id` can return
`run_not_found` even though the Run exists; do not diagnose that as a race.
`client.run(run_id)` and `client.cancel(run_id)` take the opaque Run ID
(the final path component of the returned Run URI); GRIP `describe`/`invoke`
take the full URI. Preserve both forms in reports, without inventing IDs.

Use only the documented public Observer data-plane and submission paths; do
not call internal control-plane or worker endpoints.
