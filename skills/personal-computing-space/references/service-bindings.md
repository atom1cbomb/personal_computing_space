# Service bindings (SDK 0.8 and matching server/runtime)

Discover resources and contracts through Observer. Invoke the exact offer using
an operator-trusted endpoint/audience. A binding may reach an independent
service, a Slave data gateway, or Observer relay. Component names do not select
routes. Explicit endpoints are authoritative; omitted endpoints require a unique
trusted route. Supply operator routes via `LoomClient(trusted_bindings=[...])`;
`binding_preference` can select an offered endpoint or audience. Never turn an
Observer binding into a Slave address. No replay or route fallback occurs after
sending a request.

Read the offer's mode and schemas. Package `runtime_binding.interaction` controls
its exported interface:

- `task` (default): request-reply with `{arguments, expected_revision,
  idempotency_key}` and `{run: URI}`. Poll that Run.
- `request-reply`: business input and business result, no implicit Run.
- `byte-stream`: business admission input and stream-open output, followed by
  ordered bytes. Use `await client.open_stream(target, action, input)` as an
  async context manager with `read`, `write`, `aclose`. No half-close, stream ID,
  transparent reconnection, or replay. A new channel is a new invocation.

A business request can still be explicitly submitted with `run-package` for
persistent task execution. A stream export cannot become a persistent task node.
Bindings of the same action share the same schemas/mode/result semantics.

Managed service calls exchange the Observer owner token at `/api/v1/auth/token`
for short-lived target/action/audience/activation-revision grants. The SDK caches
these, immutable schemas, and unexpired descriptions. It never forwards a
caller token to another audience or copies default HTTP auth/cookies. Public
TLS uses HTTPS/WSS; insecure HTTP/WS needs explicit operator/client opt-in.
Independent services can use a configured async `credential_provider` instead
of Loom's token issuer. Owner credentials stay in the external client.

Activation `configure` takes `expected_revision`, `idempotency_key` and optional
complete replacement `instance_config`/`dependencies`; omitted portions stay
unchanged. `dependencies` maps a name to `{target: GRIP_URI, actions: [exact URIs]}`.
The target actions must be service actions. `credential_profile` remains fixed.
Check `desired_revision`, `applied_revision`, `state`; do not assume a stored
configuration has been successfully applied. Updating MCUE attributes still does
not configure a runtime.

Packages consume only declared dependency grants through
`DependencyContext().client(name, min_valid_seconds=request_budget)`. Reserve
enough grant lifetime for a new request; insufficient lifetime fails before
sending bytes. Slave atomically refreshes the per-instance
read-only context at `/run/loom/dependencies/context.json`. It includes exact
descriptions/schemas/routes and limited short-lived credentials. Do not upload
that context or copy it into logs, Run inputs, content, or public attributes.
Failed refresh retains only the previous grant's original expiry; expired
channels close and new requests fail until credentials are refreshed.

For Kubernetes service access, use `services-ensure` for an explicitly configured
ordinary ClusterIP Service, then `service-access-status` and
`open-service-stream({service_ref})`. Keep namespace/name/port/workload UID and
selector in the activation's `service_targets`, not in the stream input. It
rejects headless/external/unowned/recreated Services. The capability owns its
port-forward process, reconnects with backoff, and aborts old streams. Kubeconfig
and kubelogin cache stay in that activation; the embedding package receives a
GRIP grant, never Kubernetes credentials.

The embedding package exposes direct `embed`/`info` and a separate native
AutoPaper adapter (`/health`, `/info`, `/embed-one`, `/embed`). Operator deployment
can publish only the consumer port to host loopback. Read actual model identity
and dimension before registering `model/embedding` attributes. `network/service`
attributes link the Service to its Kubernetes resource. Reauthentication follows
[authentication-authoring.md](authentication-authoring.md); version upgrades and
same-version restart recovery are separate operations.
