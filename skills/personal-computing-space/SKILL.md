---
name: personal-computing-space
description: Use when a coding agent needs to submit, monitor, continue, resolve, or cancel a distributed task through a deployed Personal Computing Space (Loom) Observer.
---

# Personal Computing Space

Use the Observer as the only external entry point. The external agent owns
discovery and orchestration; Driver only executes a frozen submission.

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

When a package export contains `capability_descriptors`, treat its
`resource_effects` as the authoritative composition metadata. Do not infer a
follow-up package from a provider name or from an operation's prose.

For a `create` effect, follow the declared `identity_path`, invoke the bounded
`readiness` operation with its `input_mapping`, and register the resulting
resource using the declared package and manager references. Before emitting a
node for an `access_requirements` entry, create the declared activation bound
to `target_resource_ref`, complete authentication when
`requires_authentication=true`, and wait for the declared `ready_state`.
Always retain the resource identity and use the declared `cleanup` operation on
failure, cancellation, or successful completion. A provider HTTP success or a
create response does not mean that the resource is ready for access.

The public package endpoint includes verified descriptor payloads inline. If a
descriptor is not inline, resolve it only through the public
`GET /api/v1/capability-descriptors?resource_id=...&version_or_digest=...`
endpoint. If the descriptor, output schema, or referenced package cannot be
resolved, stop with a metadata/readiness blocker instead of guessing a field,
package, or polling operation.

For Suanli VM creation, the declared workflow is therefore
`instances-run` → `instances-status` until the VM is running and has an IP →
resource registration → `suanli-vm-ssh-http` activation and browser
authentication → `exec`; retain the InstanceId for
`instances-terminate` cleanup.

## Workflow

1. Check `GET /healthz` and `GET /api/v1/runtime`. Stop and report
   `driver_unavailable` rather than bypassing the Observer.
2. Discover workspace compute resources with `GET /api/v1/slaves` before
   planning a distributed task. Use `?available_only=true` when only currently
   leased Slaves matter. Treat `available` as a recent-lease signal, not a
   promise of CPU/memory capacity or successful admission; inspect
   `base_operations`, `executor_descriptors`, and
   `runtime_plugin_descriptors` when choosing a feasible task. The endpoint is
   workspace-scoped and does not expose internal endpoints or lease tokens.
3. Upload source/input content and register packages through the public data
   plane; use `GET /api/v1/resources/{resource_id}/activations` to reuse an
   activation explicitly bound to a resource.
4. Build one complete `ClosureContract` with `SubmissionBuilder` and submit it
   with a stable `request_id`. Do not include secrets in source or closure.
5. Poll `GET /api/v1/run-submissions/{request_id}` with a bounded interval.
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

Use the gateway's fixed operation exports rather than constructing shell
commands. Call `/v1/auth/start` or `/v1/auth/status` first. If the result is
`auth_required`, show the user `verification_uri`, `user_code`, and `qr_payload`,
wait for OIDC device-code login, then poll status. Never ask for a token or
kubeconfig in a task message.

Call `/v1/jobs/submit` only with a VolcanoJob manifest
(`apiVersion: batch.volcano.sh/v1alpha1`, `kind: Job`). Use the gateway's status,
logs, and delete operations; do not submit a bare GPU Pod or invoke arbitrary
kubectl commands.
