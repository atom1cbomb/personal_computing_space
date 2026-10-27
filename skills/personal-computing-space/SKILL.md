---
name: personal-computing-space
description: Use when a coding agent needs to discover or describe Loom resources, update their public attributes, author or publish Loom capability packages including authentication, or submit and manage distributed tasks through a deployed Personal Computing Space (Loom) Observer.
---

# Personal Computing Space

Use Observer for discovery, management and task submission. Invoke resource actions
through the operator-trusted bindings in DESCRIBE; a service, Slave data gateway,
or Observer relay may implement a binding. The external agent owns orchestration;
Driver executes frozen task submissions. For service calls, streams, activation
configuration and dependency grants, read [references/service-bindings.md](references/service-bindings.md).

## Decide how an action executes

GRIP `offer.action.mode` describes the wire exchange, not whether the business
action creates a Run. Both a durable task and a direct business call can have
`mode=request-reply`. Read `resource.metadata.actions[].interaction` for the
selected action and its offered input/output schemas before calling or reporting:

- `task`: submit the declared wrapper and follow the returned `run` URI to its
  terminal state and result. This includes task exports of service packages.
- `request-reply`: pass business input and verify the direct business result.
- `byte-stream`: use the offered stream contract and `open_stream`.

The package's `runtime_binding.interaction` defaults to `task` when omitted.
Do not infer interaction from a package name, an HTTP endpoint, or wire mode.
If metadata is incomplete, read the output schema and retain uncertainty instead
of declaring that no Run or Driver is involved. Driver availability is required
for task execution, including service exports that return a Run.

For an existing named service, target that activation's URI. Slave `run-package`
does not select a named instance and falls back to `default`; it must not be used
as a substitute when that instance's action is unavailable. For a persistent Run
that waits for authentication on an existing instance, read
[references/existing-activations.md](references/existing-activations.md).

## Public SDK

Use Python 3.12+ and the independent `loom-orchestration` package. A release
skill archive includes its wheel under `assets/wheels/`; install that wheel
in the external agent's environment, not in the orchestration sandbox.
See [references/sdk.md](references/sdk.md) for installation, discovery, and
source-checkout fallback. Do not assume the package is published on PyPI.
For a new Python computation on a Slave, read
[references/function-packages.md](references/function-packages.md): publish a
single-export `process:json_stdio/1` function through public content/package APIs,
then use its discovered action. A pre-existing application package is not required.

## Resource types and public attributes

For resource descriptions, registration, or attribute updates, read
[references/mcue.md](references/mcue.md). MCUE defines the resource's public
extension attributes. The contract uses `resource.mcue` (for example,
`computing/vm`), a versioned `metadata.mcue_ref`, and `metadata.attributes`.
It replaces resource-level `kind`; it does not rename GRIP reply/error kinds.

MCUE requires SDK 0.5 and the matching Observer release with its resource
migration applied. Discover the deployed Observer's offers and schemas before
invoking actions; updating this skill alone does not upgrade a deployment.

The selected MCUE schema decides whether connection or runtime information
is a public attribute. Do not impose a universal host/user/port blacklist or
copy `instance_config` into attributes. Credentials normally remain in the
authentication/runtime credential channels. Public attributes describe a
resource; action offers and runtime configuration govern its use.

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
- Public GRIP service data bindings are distinct from internal Driver/Slave endpoints. Direct plan submission is the
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

Credentials are selected per activation. For a named service, pass the
operator-provided `credential_profile` in the Slave's `activate-package` input,
alongside `instance_config`; never put the selection in a package manifest or
copy credential files into requests or content. Different instances may select
different profiles. Omission/null preserves an existing binding, or mounts no
credentials for a new instance; changing profiles requires a new instance name.
See [references/sdk.md](references/sdk.md) for the matching server/SDK requirement
and orchestration's `ensure_activation` argument. Do not infer a profile from the
package name or from DESCRIBE, which does not expose credential configuration.

## Resource-producing capability packages

When a package export contains `action_descriptors`, treat its
`resource_effects` as the authoritative composition metadata. Do not infer a
follow-up package from a provider name or from an action's prose.

For a `create` effect, generate source that extracts the declared `identity_path`,
registers each resource and explicitly defers cleanup before readiness polling.
In the MCUE contract, use the effect's explicit `mcue_ref` for registration;
do not infer a type from a package name, URI path, or connection address.
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

1. For task submission, check `GET /healthz` and `GET /api/v1/runtime`; report
   `driver_unavailable` if task execution is unavailable. Direct request-reply and
   byte-stream business interactions use their own availability and contracts;
   task exports of service packages still require Driver execution.
2. Discover compute with `await client.resources(mcue="computing/slave")` and inspect descriptions
   for Slave `run-package` / `check-package` / `activate-package` offers. Select available leases
   using `metadata.available` and inspect executor/runtime descriptors.
   Use exact returned action URIs and the deployed action schemas.
3. Publish source/packages through the authoring data plane. Before activation,
   invoke the discovered `check-package` offer with the package URI and intended
   instance_name. Report its issues when ready=false; activation repeats this
   check before changing state. Node service entries, TLS and routing belong to
   node onboarding: never SSH to repair them as part of package deployment.
   Build/push images locally or in CI; the Slave pulls the immutable image_ref.
   A missing check-package offer requires an Observer platform upgrade, not an
   SSH fallback. Discover registered
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

## Authentication and reauthentication

When creating or updating an authenticated `service + container:http/1` package,
read [references/authentication-authoring.md](references/authentication-authoring.md).
It defines the manifest, private gateway hooks, system responsibilities,
credential isolation, expiry recovery and validation workflow. Reuse the SDK
portal; implement provider-specific authentication in the service gateway.

For browser/device login, use the SDK's single `loom-auth` portal for all selected
activation URIs. Read [references/authentication.md](references/authentication.md)
for installation, instance selection, status, expiry and retry. The portal starts
`auth-start` using the latest DESCRIBE revision and verifies ready before showing
success. Expiry does not require a new resource, activation or business Run.

## Kubernetes capability package

Use the package's fixed business actions from GRIP DESCRIBE. Activate the
package on a chosen Slave. On `pending_auth`, use the unified login portal for that activation and poll
the same activation until ready. Never ask for a token or
kubeconfig in a task message.

Invoke the discovered jobs-submit action only with a VolcanoJob manifest
(`apiVersion: batch.volcano.sh/v1alpha1`, `kind: Job`). Use its status, logs, and
delete exports. Runtime HTTP paths are private implementation bindings.

## Volcano visibility

For queues, PodGroups, cross-namespace workload visibility and GPU allocation,
discover resources with `mcue="computing/volcano"`. Their MCUE public attributes
include `dashboard_url` and `kubernetes_ref`, which identifies the associated
Kubernetes resource. Read [references/volcano.md](references/volcano.md) before
querying or connecting a Dashboard. Use its ready activation and exact discovered
action URI; Dashboard addresses differ across clusters.

## Grafana monitoring

For actual GPU utilization, host GPU numeric indexes and historical monitoring,
discover `mcue="computing/grafana"` and match its explicit `kubernetes_ref` or
`volcano_ref`. Read [references/grafana.md](references/grafana.md) before querying
or connecting it. Volcano allocation counts do not provide actual utilization.
