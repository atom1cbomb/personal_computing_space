---
name: personal-computing-space
description: Use when a coding agent needs to submit, monitor, continue, resolve, or cancel a distributed task through a deployed Personal Computing Space (Loom) Observer.
---

# Personal Computing Space

Use the Observer as the only external entry point. Act as a user of the
workspace; let the Driver's coding agent refine the closure, invoke its internal
tools, orchestrate work, and dispatch nodes to Slaves.

## Boundary

- Submit work with `POST /api/v1/messages`.
- Observe work with `/api/v1/conversations/*` and `/api/v1/runs/*` reads.
- Use `POST /api/v1/conversations/{conversation_ref}/interrupt` only to cancel.
- Resolve `awaiting_decision` only after presenting the evidence and obtaining
  the user's explicit `accept` or `abandon` decision.
- Never call Observer `/mcp`, Driver `/driver/v1/*`, Observer `/internal/v1/*`,
  or Slave `/worker/v1/*`. Those are internal control surfaces.
- Never use direct Run mutation endpoints to open, patch, commit, or start a
  Run. The Driver's coding agent owns those decisions.
- Treat `ResourceRef` and digest fields as server-owned facts. Do not calculate,
  invent, or overwrite content/package/draft digests; obtain immutable refs from
  the Observer data plane or persisted Run outcome.
- A user-owned capability package may be registered through
  `POST /api/v1/capability-packages` after its referenced contents are uploaded
  with `POST /api/v1/content`; omit lifecycle fields and package digest. This
  creates a workspace-reusable package without a Run and does not activate a
  Slave. Run-bound packages remain the Driver agent's responsibility.

For a Kubernetes task, require a ready `service + container:http/1` export whose
package contract documents the Kubernetes gateway. `available=true` alone does
not mean that a Kubernetes credential profile is configured.

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
3. Reuse one stable `conversation_ref` for follow-up turns. Generate a new
   opaque `request_id` for each semantic message.
4. Write a task message that states the goal, inputs visible to the workspace,
   required output, distribution requirement, constraints, and acceptance
   criteria. Do not include secrets.
5. Submit the message and treat HTTP `202` as receipt acceptance, not task
   completion.
6. Poll `GET /api/v1/conversations/{conversation_ref}` with a bounded interval.
   Correlate the user message by `request_id`; inspect its status, assistant
   reply, Run outcome, dynamic nodes, and evidence.
7. On `completed`, verify the requested acceptance criteria from the outcome.
   On `awaiting_decision`, follow the decision rules in the protocol reference.
   On `failed` or `interrupted`, report the structured reason without hiding it.
8. For corrections or repair, send a new message in the same conversation with
   a new `request_id`; do not mutate internal Run state directly.

Retries after an uncertain HTTP result must reuse the same `request_id` with
the exact same `conversation_ref` and text. Reusing it with different content is
an error.

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
