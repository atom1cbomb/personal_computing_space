# Observer External-Agent Workflow

The external agent is the planner. The logical GRIP Driver accepts a complete
`ClosureContract`; Observer validates, freezes and queues it. The Driver process
claims immutable execution requests and never receives prompts or conversations
for planning.

## Discovery

1. Discover `client.resources(mcue="computing/driver")`, DESCRIBE its URI, and
   read its `how-to-use` and offered schemas. There is one logical Driver at
   `drivers/default`; its URI remains stable across process restarts.
   `metadata.accepting_submissions` describes the Observer entry point, while
   `metadata.available` describes the execution lease. A Driver offline lease
   permits durable queueing; report the waiting state without treating a Run URI
   as evidence of execution. `metadata.task_format.schema` describes the accepted
   complete closure; both ordinary plans and `container:python_orchestrator/1`
   use this Driver.
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
`run` URI identifies a Run resource with lifecycle actions including `read` and `cancel`. If you need
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

SDK 0.10 implements `submit` by invoking Driver `submit-plan` with
`{plan, idempotency_key, conversation_ref?}`, then `get-submission` with
`{idempotency_key}`. An accepted action returns `{run: URI}`; the SDK helper returns the
full submission view, including `run`, `run_id`, `plan`, `closure_version`,
`snapshot_digest`, `execution_request_state`, `current_run_state` and `outcome`.
An accepted Run is durably queued; it has not necessarily started executing.
A readiness rejection instead returns `{rejected: {code, details: {blockers}, ...}}`
in the action body and creates no submission. SDK `submit`/`resubmit` raise
`PlanRejectedError` with `.code` and `.details`; inspect the blockers and repair
the plan. A key that was rejected before acceptance may be reused with corrected input.

The SDK's `request_id` parameter is the business `idempotency_key`. It is separate
from the GRIP envelope's per-message `request_id`. Retry an uncertain response
with the exact same business key, canonical plan and conversation_ref, or recover
the receipt using `await client.submission("request-opaque")`. Changed content
with the same key returns the GRIP error `request_id_reused`.

The `plan` URI names `plans/{run_id}/{closure_version}` with MCUE `execution/plan`.
Its `read` action returns `{run, closure_version, snapshot_digest, plan}`. The
digest identifies the frozen TaskClosure body; `plan` includes the complete
contract, including success requirements and execution policy. This version is
immutable; repairing a Run creates another Plan URI while the old one remains
readable.

## Poll and Authenticate

Poll `client.submission(business_key)` or invoke `read` on the returned Run URI.
The default Run read input `{}` returns state, outcome, execution identity,
Driver and current Plan references. Set `{"include_history":true}` for attempts,
events, dynamic nodes and evidence; `client.run(run_uri)` requests that view.
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

## Repair, Resolve, Cancel and Close

All lifecycle actions use Run offers through GRIP. SDK `run`, `cancel`, `resubmit`,
`resolve` and `close` accept either the returned Run URI or its opaque Run ID.
GRIP `describe` and `invoke` take the full URI; keep returned identities intact.

Repair requires `awaiting_decision` with `outcome.decision=repair`. Invoke
`resubmit` with a complete replacement `ClosureContract`, a new business
`idempotency_key`, and current `closure_version` as `base_closure_version`:

```python
repaired = await client.resubmit(
    queued["run"], request_id="repair-1",
    base_closure_version=current["closure_version"], plan=replacement_plan,
)
```

The Run URI stays fixed; the Plan URI and closure version change. Preserve goal,
accepted success criteria, constraints and execution policy. A stale version
returns `version_conflict`; repair cannot silently weaken acceptance criteria,
expand effects or increase accepted execution budgets. Retry the exact same
repair payload and business key after a lost reply.

For `outcome.decision=attestation`, present the execution evidence and apply the
user's `accept` decision with `await client.resolve(run_uri, "accept")`.
`await client.resolve(run_uri, "abandon")` abandons a pending attestation or
repair. An execution result awaiting attestation is not accepted success.

Cancel on the user's request with `await client.cancel(run_uri, reason="user_cancel")`.
Its optional `request_id` maps to `idempotency_key` and checks that a known
submission belongs to this Run; it is not a new cancellation identifier.
Omitting it works for complete-plan, `run-package` and activation-task Runs.
`run-package` and activation task keys are scoped to those actions; do not pass
them as a Driver submission key. Cleanup may continue after cancellation, so
poll through `cleaning_up` and inspect `outcome.cleanup`.

`await client.close(run_uri)` closes a completed, failed or cancelled Run.
Repeated close is idempotent; it does not destroy external resources. Lifecycle
offers remain discoverable for retries, with state conditions checked by Observer.

Use public GRIP offers and the content/package authoring data plane. The old
task REST submission, single-Run read and lifecycle routes have been removed.
Internal control-plane and worker endpoints are not public task APIs.
