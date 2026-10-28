# Service bindings (SDK 0.8 and matching server/runtime)

Discover resources and contracts through Observer. Invoke the exact offer using
an operator-trusted endpoint/audience. A binding may reach an independent
service, a Slave data gateway, or Observer relay. Component names do not select
routes. Explicit endpoints are authoritative; omitted endpoints require a unique
trusted route. Supply operator routes via `LoomClient(trusted_bindings=[...])`;
`binding_preference` can select an offered endpoint or audience. Never turn an
Observer binding into a Slave address. No replay or route fallback occurs after
sending a request.

Before activating a package, call the Slave's discovered `check-package` with
`{package: package_uri, instance_name: instance}`. It returns `ready`,
`required_interactions` and `issues` (code, interaction, affected action URIs).
Missing keys/entries or invalid bindings stop activation before state changes.
Node DESCRIBE includes `service_interactions` as a general configuration summary;
check-package also accounts for activation-specific routes. This does not probe
network reachability, pull an image or authenticate the provider. Node setup is
separate from package deployment and must not become an SSH fallback.

Activation metadata distinguishes `runtime_state` from public `state` and
`available`. A runtime-ready instance with missing service bindings is publicly
degraded; inspect `service_access.issues` and each action's availability. Existing
usable actions remain offered. Restoring routes restores availability without
recreating the instance. Stop remains available during a binding outage.

Read the offer's mode and schemas together with the selected declaration's
`resource.metadata.actions[].interaction`. GRIP `mode=request-reply` alone does
not mean direct business execution or no Run: task admission uses the same wire
mode. Package `runtime_binding.interaction` controls its exported interface:

- `task` (default): request-reply with `{arguments, expected_revision,
  idempotency_key}` and `{run: URI}`. Poll that Run.
- `request-reply`: business input and business result, no implicit Run.
- `byte-stream`: business admission input and stream-open output, followed by
  ordered bytes. Use `await client.open_stream(target, action, input)` as an
  async context manager with `read`, `write`, `aclose`. No half-close, stream ID,
  transparent reconnection, or replay. A new channel is a new invocation.

For any package, the declared interaction and output schema determine whether
the response is a Run reference or business data. If declarations and schemas
disagree, report the conflicting evidence rather than choosing an interpretation
from the mode alone.

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

Any service-specific dependency, stream setup, target identity, routing policy
or result interpretation belongs to the selected package's descriptor. Read
its declared dependency grants and service actions before configuring an
activation. Keep private provider credentials inside the activation and pass
only the package-declared GRIP grant to a dependent package. Reauthentication,
version upgrades and restart recovery remain separate operations.
