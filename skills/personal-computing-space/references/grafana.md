# Grafana monitoring resources

Use Observer GRIP and discover `mcue="computing/grafana"`. Match its explicit
`metadata.attributes.kubernetes_ref` or `volcano_ref`; do not derive monitoring
URLs from a Kubernetes/Volcano hostname. Public `dashboard_url` and private
runtime API origin can differ because of ingress ports and split DNS.

DESCRIBE the resource, choose its bound ready activation, and DESCRIBE that
activation for exact action URIs and its current revision. Invoke the resource
or selected activation with `arguments`, `expected_revision` and a stable
idempotency_key, then read the returned Run until terminal.

## Discover and query metrics

The base exports are datasources_list, dashboards_list, dashboard_get,
metrics_query, metrics_query_range and gpu_utilization. v5 additionally exports
gpu_arena_describe, gpu_arena_query and gpu_arena_gpu_hours. All are read-only.
Use dashboard_get to inspect panel expressions and variable definitions and
datasources_list to obtain actual Prometheus data source UIDs. Generic metric
queries accept PromQL and an optional data source UID, never an arbitrary URL.
Range queries require start/end as Unix seconds or timezone-qualified RFC3339,
and step as seconds; one series can contain at most 11000 samples.

gpu_utilization takes hostname and optional numeric gpu_index/time. Supply the
verified datasource_uid and gpu_metric mapping in action arguments after
discovery, or use activation defaults. The mapping identifies the metric,
node/index labels, optional UUID label and source unit. Do not guess these from common DCGM names
or other clusters. The result includes actual sample timestamps, numeric GPU
indexes, available UUIDs, utilization_percent and source labels. An empty query
is status=no_data, not 0%; warnings make complete=false. Do not infer missing
physical device identity, sum percentages from multiple series, or call GPU busy
percentage theoretical FLOPS utilization. Pod GPU requests alone do not map a
Pod to a numeric device index.

## GPU Arena (grafana-http/v5)

Use gpu_arena_describe first to discover the actual `gpu-arena` dashboard's
panels, Prometheus target refs, units, variables and public link. Use
gpu_arena_query with panel_id/ref_id and optional start/end/step, mode
(`instant` or `range`), and literal node/model/gpu_uuid arrays. Omitted filters or
["$__all"] select all. The default window is seven days; selected windows are
bounded to 90 days and at most 11000 range samples per series. Upstream retention
and telemetry determine available history. Empty data is no_data, and warnings
make complete=false. SQL display targets are explicitly unsupported by this
generic panel query; actual Prometheus expressions and samples remain in results.

The current dashboard covers live/historical allocation, per-GPU utilization,
memory usage/utilization, SM activity, temperature and power. Do not treat unknown
history as unallocated or infer Volcano schedulability from idle telemetry.

gpu_arena_gpu_hours implements the dashboard's verified namespace ranking:
allocated GPU hours × installed VRAM GiB / 80. The underlying queries deduplicate
same-namespace Pods and split cross-namespace sharing. The dashboard's accounting
step (currently 30 seconds) stays independent of the range sampling step. Missing
capacity stays unranked with null normalized hours and explicit unrated hours.
The response retains both Prometheus queries as evidence. Namespace is not a
verified student identity; A100 80GB-equivalent hours are VRAM-weighted allocation,
not compute-performance equivalence. A changed SQL formula is an explicit adapter
compatibility error, never permission to guess a new normalization rule.

## Credentials and activation

Use grafana-http/v5 for GPU Arena, unified login and automatic session token rotation.
For QR login, activate without credential_profile, then
use the [unified authentication client](authentication.md). The SDK opens an
isolated visible browser on the user's machine and follows Grafana's original
OAuth/WeChat QR flow. It returns only that context's declared cookie through
the Observer bridge. Do not ask users to export cookies or create session files.
Retry expired/failed logins from the same portal; verify the same activation
becomes ready. The package declares browser-cookie interaction.

Grafana's `401` with `messageId=session.token.rotate` requests normal token
rotation, not a new human login. v4 renews the cookie through the fixed
same-origin endpoint and retries the read once, including health probes.
v3 deployments lack this step and may repeatedly enter pending_auth after about
ten minutes. Upgrade the gateway/package rather than repeatedly requesting QR
login or extending the five-minute browser challenge. A true expired/revoked
session still needs login. Discover the deployed version and exact activation
URI; source/skill changes alone do not upgrade production.

Each activation has its own persistent private session volume. Non-interactive
deployments can select credential_profile containing grafana_token (read-only
service-account Bearer token) or grafana_session (cookie value only). The runtime
mounts it read-only. Never put credentials in descriptions, business action
parameters, uploads, URLs, logs or chat.

These Grafana APIs require authentication independently of Kubernetes and
Volcano. The helper preserves the Grafana-registered callback and does not
replace it with localhost or invent a device-code grant for that client.

Register a computing/grafana resource and bind grafana-http's target to it.
instance_config requires api_origin; browser_origin is the public browser origin
when routing differs, and datasource_uid/gpu_metric are optional query defaults.
Use verified configuration and exact Observer-returned refs; the source helper
is scripts/connect_grafana.py. Updating public attributes does not reconfigure
an existing activation's connection or credential binding.
