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
SDK 0.9 includes `inventory`, `action_contract`, `execute_action`, `CallLedger`,
`publish_function`, `submit_map_reduce` and `wait_run`. Prefer these public
helpers for resource discovery, ordinary JSON calls, bounded multi-call tasks
and map/reduce computations; see [references/reliable-workflows.md](references/reliable-workflows.md).
They preserve exact contracts and evidence while reducing repeated protocol code.
Start discovery with the compact inventory; keep pending/degraded instances in
the assessment, and inspect action descriptions before declaring a capability absent.
Every public GRIP directory entry also offers the platform `how-to-use` read action.
After DESCRIBE, invoke it when the relationship between a resource, its package,
managed-by resource, activation, custom attributes, or callable actions is unclear;
use its exact action and schema references instead of inferring provider behavior.
`how-to-use` explains dependency fields and their meaning. Read actual runtime
references from DESCRIBE or the workspace `list-resources` action's
`metadata.dependencies`; `null` means no dependency declaration, not proof of
independence. The protocol still has only DESCRIBE and INVOKE.
Capability packages published by `ServicePackage` expose their package
`HOW_TO_USE.md` through the returned `documentation` field (backed by the immutable
`how_to_use_ref` content reference). Treat that document as package-specific usage
guidance; the package `README.md` is for build/deployment details, and the action and
schema references in the response remain authoritative.
For a reproducible failure, read [references/diagnostics.md](references/diagnostics.md)
to inspect the exact package's published source without changing its deployment.
For a new Python computation on a Slave, read
[references/function-packages.md](references/function-packages.md): publish a
single-export `process:json_stdio/1` function through public content/package APIs,
then use its discovered action. A pre-existing application package is not required.

## Resource types and public attributes

For resource descriptions, registration, or attribute updates, read
[references/mcue.md](references/mcue.md). MCUE defines the resource's public
extension attributes. The contract uses the returned `resource.mcue`, a
versioned `metadata.mcue_ref`, and `metadata.attributes`.
It replaces resource-level `kind`; it does not rename GRIP reply/error kinds.

MCUE requires SDK 0.5 and the matching Observer release with its resource
migration applied. Discover the deployed Observer's offers and schemas before
invoking actions; updating this skill alone does not upgrade a deployment.

The selected MCUE schema decides whether connection or runtime information
is a public attribute. Do not impose a universal host/user/port blacklist or
copy `instance_config` into attributes. Credentials normally remain in the
authentication/runtime credential channels. Public attributes describe a
resource; action offers and runtime configuration govern its use.

## Capability package descriptions

For the concrete resolution procedure and relationship checks, read
[references/capability-descriptors.md](references/capability-descriptors.md).

The project skill is provider-neutral. It defines how to discover and verify a
capability; it does not define what a particular VM, cluster, dashboard,
model, or document service does. Treat the selected capability package as the
source of that behavior:

1. Enumerate the relevant resources and activations through the public
   Observer API. Keep the exact resource and activation URIs, package URI,
   revision, `managed_by`, `state`, and public attributes as evidence.
2. Resolve the returned `metadata.mcue_ref` through its exact public MCUE read
   action before interpreting attributes. DESCRIBE the selected resource and
   activation, then read the offered action, input/output schema, interaction
   mode, permissions, dependency grants and any inline descriptor or
   `resource_effects` before constructing a call.
3. If a descriptor is referenced but not inline, fetch it through the public
   `GET /api/v1/action-descriptors?resource_id=...&version_or_digest=...`
   action using the returned resource ID and version/digest. Resolve
   every referenced schema, contract, package and action the same way. A
   missing or conflicting descriptor is a metadata blocker; do not guess names,
   fields, lifecycle states, identity links, endpoints, or provider semantics.
4. Use the package-declared identity and relationship paths to match resources
   and action results exactly. `managed_by` and other system metadata are
   evidence for ownership/provenance; they do not replace a package-declared
   identity mapping. An address, display name, package name or URI spelling is
   never a relationship proof.
5. Follow the descriptor's interaction, readiness, authentication, side-effect,
   retry and cleanup declarations. Record the exact action, arguments, result,
   Run/content references and final state. Never substitute a locally inferred
   result for the declared remote operation.

Capability packages may expose their current public source or additional
documentation through the package's declared references. Read those only via
the public package/content actions and treat them as immutable evidence for the
selected package version. The source of truth is still the deployed descriptor
and schemas; project-level examples must not override them.

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
Resolve that type's dependency fields and the effect's `attributes_mapping`.
Use `loom_orchestration.resource_attributes(effect, arguments=..., result=...,
instance_config=...)` to fill declared public attributes, then register them.
Required dependency values must be supplied; do not invent or infer a host URI.
See [references/mcue.md](references/mcue.md) for the schema annotation and
publication checks.
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
descriptor is not inline, resolve it only through the public descriptor action
with the returned resource ID and version/digest. If the descriptor, output
schema, or referenced package cannot be resolved, stop with a
metadata/readiness blocker instead of guessing a field, package, or polling
action.

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
   instance_name and proposed instance_config. Report its issues when ready=false; activation repeats this
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

## Capability-specific behavior

There are no project-level sections for individual providers or resource
classes. For a concrete service, use the package and MCUE descriptors returned
by Observer, then apply the generic discovery and contract rules above. A
package may document provider-specific authentication, actions, result
semantics and lifecycle in its own immutable descriptor or public `HOW_TO_USE.md`; that
documentation travels with the package rather than with this skill.
