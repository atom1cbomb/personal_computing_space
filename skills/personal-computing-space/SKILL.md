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
Complete plans use the discovered `computing/driver` resource's GRIP `submit-plan`
action. This is one logical Driver whose resource URI survives process restarts.

## Decide how an action executes

Ordinary JSON actions support two execution choices. Read the selected action's
`resource.metadata.actions[].execution` and business schemas before calling:

- Prefer direct execution for exploration, tests and simple reads: if
  `execution.direct.available`, pass business input to `client.invoke()` and
  verify the returned business result. No Run, durable Attempt or automatic
  result content is created. The caller handles uncertain outcomes, retries
  and business idempotency; a GRIP request ID is not an idempotency key.
- Prefer task execution when records, scheduling or recovery are needed: if
  `execution.task.available`, bind the action in a complete plan and use
  `client.submit()` / Driver `submit-plan`. Save the plan and stable submission
  key before sending. Follow the Run and verify its persisted result. See the
  [single-node plan template](references/single-action-plan.md).
- `interaction=byte-stream` uses `open_stream()` and cannot be a task node.
  Python orchestration programs run through a complete plan with Run context.

`runtime_binding.interaction` defaults to `request-reply`; it describes the
business exchange. The agent chooses direct or task, including for the same
action. `execution.task` describes action-local readiness; check the Driver's
submission status separately. Missing direct routes need not block task use.
Do not fall back to a task after an uncertain direct call: it may repeat effects.

Direct calls may exceed 30 seconds: configure the SDK wait with `timeout=None`
or a suitable duration. Explicit request deadlines and grant expiry still bound
server execution. Duration alone is not a reason to choose a Run.

A Run recovers at recorded node boundaries. It does not checkpoint an action's
internal A/B/C calls. An action must handle its internal failures or, with the
necessary authorization, submit an explicit child plan for separately recoverable steps; Run execution alone does
not provide exactly-once side effects.

## Public SDK

Use Python 3.12+ and the independent `loom-orchestration` package. A release
skill archive includes its wheel under `assets/wheels/`; install that wheel
in the external agent's environment, not in the orchestration sandbox.
Before first use, check that `loom-orchestration` is installed and importable in
the chosen Python environment; a source checkout alone is not an installation.
If it is missing, explicitly tell the user that the Loom Python SDK must be
installed, provide the Observer's matching wheel download URL and installation
command, and install within the task's existing authorization. Do not silently
substitute server imports or leave the missing dependency unexplained.
Observer releases serve this skill at `/downloads/personal-computing-space.zip`
and the SDK filename, version and SHA-256 at `/downloads/manifest.json`.
Download the wheel at `/downloads/<sdk.filename>` from the operator-provided
Observer URL; refresh this manifest after an Observer upgrade.
See [references/sdk.md](references/sdk.md) for installation, discovery, and
source-checkout fallback. Do not assume the package is published on PyPI.
SDK 0.9 includes `inventory`, `action_contract`, `execute_action`, `CallLedger`,
`publish_function`, `submit_map_reduce` and `wait_run`. Prefer these public
helpers for resource discovery, ordinary JSON calls, bounded multi-call tasks
and map/reduce computations; see [references/reliable-workflows.md](references/reliable-workflows.md).
They preserve exact contracts and evidence while reducing repeated protocol code.
SDK 0.10 moves `submit`, `submission`, `run`, `cancel`, `resubmit`, `resolve`
and `close` onto GRIP. Use the matching Observer release; the old task REST routes
are removed. Run helpers accept a returned Run URI or an opaque Run ID.
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
   Observer API. Keep the exact resource and activation URIs, the activation's
   package URI and target, revision, `managed_by`, `state`, and public attributes
   as evidence. Domain resources do not carry a package reference.
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

- Discover `computing/driver` and submit a complete `ClosureContract` with its
  `submit-plan` action, using `loom-orchestration` or the offered GRIP contract.
- Supply a stable business `idempotency_key`, optional `conversation_ref`, and
  complete `plan`. The returned `{run: URI}` confirms durable acceptance, not
  execution. SDK `submit(request_id=...)` maps that parameter to the business
  key; the GRIP message `request_id` is independently generated per exchange.
- Observe work with Driver `get-submission` (`client.submission(key)`) and Run
  `read` (`client.run(run_uri)` for state; use `include_history=True` and
  `history_after`/`history_limit` for paged history). Read the returned Plan
  URI for its immutable `ClosureContract`, closure version and snapshot digest.
- Resolve `awaiting_decision` only after presenting the evidence and obtaining
  the user's explicit `accept` or `abandon` decision.
- Never call Observer `/mcp`, Driver `/driver/v1/*`, Observer `/internal/v1/*`,
  or Slave `/worker/v1/*`. Those are internal control surfaces.
- Public GRIP service data bindings are distinct from internal Driver/Slave
  endpoints. Driver `submit-plan` is the public complete-plan entry point;
  the Driver Pool owns validation, freezing, Run mutation, history and enqueue.
  Trust its operator-provided binding explicitly; Driver processes execute
  through internal Slave bindings without an Observer execution relay.
- Treat `ResourceRef` and digest fields as server-owned facts. Do not calculate,
  invent, or overwrite content/package/draft digests; obtain immutable refs from
  the Observer data plane or persisted Run outcome.
- A user-owned capability package may be registered through
  `POST /api/v1/capability-packages` after its referenced contents are uploaded
  with `POST /api/v1/content`; omit lifecycle fields and package digest. This
  creates a workspace-reusable package without a Run and does not activate a
  Slave. Run-bound packages are part of the complete external submission.

Run-owned packages and activations are visible only with their owning Run
context. To share them across Runs, explicitly publish a workspace package,
activate a new workspace instance, verify it, then stop the private instance.
Read [capability scope and migration](references/capability-scope.md) for the
scoped package APIs and the activation migration sequence.

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

1. Discover `computing/driver` and inspect DESCRIBE, `how-to-use`, and the
   `submit-plan` schemas. Its `accepting_submissions` describes the Observer
   entry point; `available` describes the Driver lease. Offline execution does
   not prevent durable submission: report that accepted work is queued pending
   a Driver. Direct request-reply and byte-stream business interactions use
   their own availability and contracts; task execution uses a Driver.
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
   through `client.submit(request_id=business_key, plan=plan)`. Save `run`, `plan`,
   `closure_version`, and the key. Do not include secrets in source or closure.
5. Poll `client.submission(business_key)` or the Run's `read` action at a bounded
   interval; request `include_history=true` when evidence is needed.
   On `awaiting_auth`, use the exact package, instance, Slave and revision in
   `waiting` to start authentication once, then poll the same Run. Health updates
   resume it automatically; do not submit another Run or resubmit for login.
6. On `completed`, verify the requested acceptance criteria from the outcome.
   On `awaiting_decision`, follow the decision rules in the protocol reference.
   On `failed` or `interrupted`, report the structured reason without hiding it.
7. For `awaiting_decision` with `outcome.decision=repair`, invoke Run `resubmit`
   with a complete replacement plan, a new `idempotency_key` and the current
   `closure_version` as `base_closure_version`. `client.resubmit` wraps this and
   returns the new submission view. Preserve the accepted success contract,
   constraints and execution policy; the Run URI stays fixed and its Plan URI
   changes. For attestation use `resolve(accept)` after the user's decision;
   `resolve(abandon)` abandons pending work. Close terminal Runs with `close`.

Retries after an uncertain GRIP result must reuse the same business key, plan
and conversation reference. Reusing the key with different content is an error.
The transport message ID may change. `get-submission` recovers a lost reply.

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
