# Grafana monitoring capability package

`grafana-http/v4` reads Grafana dashboards and queries their Prometheus data
sources. GPU queries identify the node and the host GPU's numeric index, retain
an available UUID, and report actual sampled utilization. Publish and invoke
through Observer GRIP using the [shared service workflow](../README.md).

| action | Arguments |
|---|---|
| datasources_list | none |
| dashboards_list | optional search |
| dashboard_get | uid |
| metrics_query | query; optional datasource_uid, time |
| metrics_query_range | query, start, end, step; optional datasource_uid |
| gpu_utilization | hostname; optional gpu_index, time, datasource_uid, gpu_metric |

All six exports are SafeRead with `grafana.read`. Metrics actions use Grafana's
Prometheus data source proxy; callers cannot provide an arbitrary URL, HTTP
method or upstream path. The package uses the existing `/api` endpoints
supported by the deployed Grafana. No Grafana dashboards or data sources are
created or modified.

## Instance configuration

The API origin is private per-activation configuration. Public Grafana URLs
belong to `computing/grafana` MCUE attributes, alongside `kubernetes_ref` and
optional `volcano_ref`. Public attribute changes do not reconfigure the service.
Use the actual operator-provided runtime origin; do not rewrite ports in code.

```json
{
  "api_origin": "https://grafana-internal.example",
  "browser_origin": "https://grafana-public.example:7443",
  "datasource_uid": "uid-returned-by-grafana",
  "gpu_metric": {
    "name": "DCGM_FI_DEV_GPU_UTIL",
    "node_label": "Hostname",
    "index_label": "gpu",
    "uuid_label": "UUID",
    "unit": "percent"
  }
}
```

The metric configuration above is an example. Discover the real data source UID,
dashboard panel expression, node/index labels and source unit before configuring
it. `gpu_metric` and `datasource_uid` are optional so a Grafana instance can be
used for general dashboard/metric discovery; `gpu_utilization` requires a verified
`gpu_metric`, either from instance defaults or explicit action arguments. The
query arguments allow a newly logged-in instance to discover and then query its
actual metrics without recreating its activation. Without a configured data source, generic queries select the unique
default Prometheus data source, or the sole Prometheus data source. Ambiguity is
an error.

## Authentication

By default, activate without credential_profile. The activation starts in
pending_auth. Invoke its auth-start action with the current revision, then use
the [unified SDK login client](../../docs/authentication.md).
The client opens an isolated, visible Chromium window on the user's machine,
which follows Grafana → connect.cs.ac.cn → open.cs.ac.cn's WeChat QR sign-in.
Grafana handles its own OAuth state, PKCE and registered callback. No redirect
URI is rewritten. The client reads only the cookie from its own browser context
and sends it in the existing authenticated Observer bridge callback POST body.
The gateway validates data source access before saving it privately and the
normal health probe makes that same activation ready.

Browser login challenges are bound to the selected instance/revision, expire
after five minutes, and accept a callback once. This deadline is independent
of the Grafana login session lifetime and its short-lived token rotation interval.
Cookies are not placed in URLs, public
attributes, action arguments, Runs, uploads or logs. Each activation enables
persistent_state and stores its verified, rotating cookie with mode 0600 in its
own service volume. The browser dependency is local to the login client; the
service image continues to use only httpx.

Non-interactive deployments can select top-level `credential_profile`.
Each private profile directory contains exactly one of:

- `grafana_token`: a read-only Grafana service account token, used as Bearer auth.
- `grafana_session`: the value of an already authenticated `grafana_session`
  cookie for that Grafana instance, without the `grafana_session=` prefix.

Keep credential files out of package manifests, public resource attributes,
instance_config, action arguments, source uploads and chat. Mount the private
profile read-only at `/run/loom/credentials`; the container runs as UID/GID 65534,
so grant that identity read access. Cookies refreshed by Grafana are saved with
mode 0600 in the activation's existing persistent service volume. A new source
cookie file supersedes saved rotation state. Different activations have separate
profiles and volumes. Tokens/cookies are never returned in action results or
gateway errors.

On HTTP 401 with Grafana's `messageId=session.token.rotate`, v4 automatically
POSTs to the fixed same-origin `/api/user/auth-tokens/rotate` endpoint, saves the
new cookie, and retries the original GET once. Health and business reads share
the instance lock so they do not rotate concurrently. The usual roughly
ten-minute token rotation does not require another QR login. Actual session
expiry/revocation still requires authentication; network/permission/rotation
failures remain service errors. Service account Bearer tokens do not use this
cookie rotation flow. Existing v3 gateways need a new immutable package
and activation to acquire this fix; updating the SDK alone is insufficient.

Missing or expired login keeps the activation pending_auth. Start a new browser
login for a browser-managed activation. For a private profile, replace the file
and let the normal health probe verify it; this mode does not start an
interactive browser login. The original cookie-file-only v1 package is retained
as immutable publication history; use v4 for unified login and automatic rotation.

## Query results

Generic queries retain Prometheus result_type, native result, warnings, infos,
query parameters and query time. A warning makes `complete=false`. Range queries
accept Unix seconds or timezone-qualified RFC3339 timestamps; step is seconds.
A single series is limited to 11000 samples. Data source lists omit backend
connection settings and secrets; dashboard lists verify pagination.

`gpu_utilization` returns `unit=percent`, the metric, query time and `samples`.
Each sample has hostname, numeric gpu_index, optional gpu_uuid, sampled_at,
utilization_percent and original labels. Unit `ratio` is explicitly multiplied
by 100; `percent` is retained. Missing samples produce `status=no_data`, never
zero utilization. Invalid device identities or non-finite/out-of-range values
fail explicitly. Multiple source series retain their own labels; no summation
of GPU percentages is performed.

The percentage measures the source metric's GPU busy utilization. It does not
measure achieved FLOPS divided by theoretical peak. The node/index identity is
the monitoring label identity; connecting a particular Pod to that GPU requires
a verified Pod/device mapping, not its GPU request count.

## Publish and connect

Build this directory as the Docker context, push the image, and publish with
its immutable digest using `scripts/publish_service_package.py`. Then use
`scripts/connect_grafana.py` with the returned package and Slave URIs, exact
Kubernetes/Volcano refs, explicit API origin and optional non-interactive credential profile and
`--query-config` JSON containing verified datasource_uid/gpu_metric. The helper
publishes MCUE, registers the named resource, and binds a named activation.
Read activation DESCRIBE to obtain current revision and exact action URIs.
