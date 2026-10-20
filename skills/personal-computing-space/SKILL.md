---
name: personal-computing-space
description: Use when a coding agent needs to submit, monitor, continue, resolve, or cancel a distributed task through a deployed Personal Computing Space (Loom) Observer.
---

# Personal Computing Space

Use the Observer as the only external entry point. The external agent owns
discovery and orchestration; Driver only executes a frozen submission.

## Public SDK

Use Python 3.12+ and the independent `loom-orchestration` package. A release
skill archive includes its wheel under `assets/wheels/`; install that wheel
in the external agent's environment, not in the orchestration sandbox.
See [references/sdk.md](references/sdk.md) for installation, discovery, and
source-checkout fallback. Do not assume the package is published on PyPI.

## Boundary

- Submit a complete plan with `POST /api/v1/run-submissions` using the
  `loom-orchestration` package.
- For a plan submission, provide a stable `request_id`, optional
  `conversation_ref`, and the complete `plan`. HTTP `202` means queued, not
  executed. Poll `GET /api/v1/run-submissions/{request_id}` or
  `GET /api/v1/runs/{run_id}`.
- Observe work with `/api/v1/run-submissions/*` and `/api/v1/runs/*` reads.
- Resolve `awaiting_decision` only after presenting the evidence and obtaining
  the user's explicit `accept` or `abandon` decision.
- Never call Observer `/mcp`, Driver `/driver/v1/*`, Observer `/internal/v1/*`,
  or Slave `/worker/v1/*`. Those are internal control surfaces.
- Do not use internal Driver or Slave endpoints. Direct plan submission is the
  supported public planning path; the Observer owns Run mutation and enqueue.
- Treat `ResourceRef` and digest fields as server-owned facts. Do not calculate,
  invent, or overwrite content/package/draft digests; obtain immutable refs from
  the Observer data plane or persisted Run outcome.
- A user-owned capability package may be registered through
  `POST /api/v1/capability-packages` after its referenced contents are uploaded
  with `POST /api/v1/content`; omit lifecycle fields and package digest. This
  creates a workspace-reusable package without a Run and does not activate a
  Slave. Run-bound packages are part of the complete external submission.

For a Kubernetes task, require a ready `service + container:http/1` export whose
package contract documents the Kubernetes gateway. `available=true` alone does
not mean that a Kubernetes credential profile is configured.

## Resource-producing capability packages

When a package export contains `action_descriptors`, treat its
`resource_effects` as the authoritative composition metadata. Do not infer a
follow-up package from a provider name or from an action's prose.

For a `create` effect, generate source that extracts the declared `identity_path`,
registers each resource and explicitly defers cleanup before readiness polling.
Use readiness `result_path` and its item-relative `identity_path` to match the
correct resource before testing conditions. Follow the declared `input_mapping`.
Before emitting a
node for an `access_requirements` entry, create the declared activation bound
to `target_resource_ref`, complete authentication when
`requires_authentication=true`, and wait for the declared `ready_state`.
Always retain the resource identity and use the declared `cleanup` action on
failure, cancellation, or successful completion. A provider HTTP success or a
create response does not mean that the resource is ready for access.

The public package endpoint includes verified descriptor payloads inline. If a
descriptor is not inline, resolve it only through the public
`GET /api/v1/action-descriptors?resource_id=...&version_or_digest=...`
endpoint. If the descriptor, output schema, or referenced package cannot be
resolved, stop with a metadata/readiness blocker instead of guessing a field,
package, or polling action.

For Suanli VM creation, the declared workflow is therefore
`instances-run` → resource and cleanup registration →
`instances-list` until the matching VM is running and has an IP →
`suanli-vm-ssh-http` activation and browser
authentication → `exec`; retain the InstanceId for
`instances-terminate` cleanup.

## Workflow

1. Check `GET /healthz` and `GET /api/v1/runtime`. Stop and report
   `driver_unavailable` rather than bypassing the Observer.
2. Discover compute with `await client.resources(kind="slave")`; select available
   leases using each resource's `metadata.available`. Inspect executor and runtime
   descriptors, then `describe` the selected URI to obtain its callable offers.
3. Publish source/packages through the authoring data plane. Discover registered
   resources and activations through GRIP. A resource description lists the exact
   ready activation URIs bound to it; choose explicitly if more than one exists.
4. Build one complete `ClosureContract` with `SubmissionBuilder` and submit it
   with a stable `request_id`. Do not include secrets in source or closure.
5. Poll `GET /api/v1/run-submissions/{request_id}` with a bounded interval.
   On `awaiting_auth`, use the exact package, instance, Slave and revision in
   `waiting` to start authentication once, then poll the same Run. Health updates
   resume it automatically; do not submit another Run or resubmit for login.
6. On `completed`, verify the requested acceptance criteria from the outcome.
   On `awaiting_decision`, follow the decision rules in the protocol reference.
   On `failed` or `interrupted`, report the structured reason without hiding it.
7. For `awaiting_decision` with `outcome.decision=repair`, submit a complete
   replacement with `POST /api/v1/runs/{run_id}/resubmit`, using a new
   `request_id` and the returned `base_closure_version`. Preserve the accepted
   success contract and execution policy.

Retries after an uncertain HTTP result must reuse the same `request_id` and
canonical plan. Reusing it with different content is an error.

Read [references/observer-workflow.md](references/observer-workflow.md) before
calling the Observer. It contains request shapes, polling rules, decision
handling, result retrieval, and a distributed-task prompt template.

## Kubernetes capability package

Use the package's fixed business actions from GRIP DESCRIBE. Activate the
package on a chosen Slave. On `pending_auth`, invoke that activation's
`auth-start` action with its `expected_revision`; show the returned public login
instructions and poll the same activation until ready. Never ask for a token or
kubeconfig in a task message.

Invoke the discovered jobs-submit action only with a VolcanoJob manifest
(`apiVersion: batch.volcano.sh/v1alpha1`, `kind: Job`). Use its status, logs, and
delete exports. Runtime HTTP paths are private implementation bindings.
