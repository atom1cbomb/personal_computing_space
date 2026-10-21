# Volcano Dashboard resources

Volcano visibility uses the independent `volcano-http` package and the Dashboard
JSON API. It does not inherit the namespace policy or credentials of the
Kubernetes gateway. The currently deployed read APIs do not require browser
login; report actual authentication/permission errors if this changes.

## Discover and query

1. Use `client.resources(mcue="computing/volcano")`.
2. Match the exact `metadata.attributes.kubernetes_ref` to the requested
   Kubernetes URI. These are explicit links; do not infer a Dashboard from
   Kubernetes hostnames, a package name, or geography.
3. DESCRIBE the Volcano resource. Its public attributes include the HTTPS
   `dashboard_url`, including its port. Its `metadata.activations` lists ready
   bindings; select explicitly when multiple bindings are present.
4. DESCRIBE the selected activation. Choose the desired action from
   `metadata.actions` and its matching callable offer; pass that activation's
   `metadata.revision` as `expected_revision` with `arguments` and a stable
   `idempotency_key`. Poll the returned Run using its public read action.

The six actions are namespaces_list, queues_list, podgroups_list, pods_list,
jobs_list and gpu_usage. Namespace defaults to `all`; pass a namespace explicitly
to narrow it. Lists report `items`, `total_count`, `complete=true`. An incomplete
or failed query is not evidence of zero jobs or zero GPUs.

`gpu_usage` defaults to nvidia.com/gpu and reports scheduled, non-terminal Pod
requests as `allocated`, the Running subset as `running_allocated`, and queued
unbound requests as `unscheduled_requested`. Results include the timestamp,
namespace, resource_name, per-namespace totals and GPU Pod summaries. Do not
report this as physical cluster capacity or GPU compute utilization. MIG and
other resource names represent separate units.

## Connect a new Dashboard

Use the operator-provided Dashboard origin and the Observer-returned Kubernetes
activation URI. Publish/discover `computing/volcano/v1`, register a named resource
with `dashboard_url` and `kubernetes_ref` attributes, and activate `volcano-http`
with `target` equal to that resource and
`instance_config={"api_origin": "https://host:port"}`. The public Dashboard URL
and runtime API origin may differ because of split DNS or ingress ports; use
the operator-provided API origin rather than rewriting a public URL's port.
The descriptor declares
the instance config schema; discover it before invoking. These public endpoints
currently need no credential_profile. Public attribute edits do not reconfigure
an activation. A source-checkout helper is `scripts/connect_volcano.py`.

The runtime must support service-owned JSON config; older HTTP runtime versions
incorrectly require SSH fields for all services. Do not bypass Observer to
work around a runtime/configuration blocker.
