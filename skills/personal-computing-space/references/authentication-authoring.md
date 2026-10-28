# Authoring authenticated capability packages

Applies to `service + container:http/1` with SDK 0.7 and the matching
Observer/Slave HTTP runtime. This is the implemented protocol; installing the
skill or SDK alone does not add server support. For user login commands, see
[authentication.md](authentication.md); for publishing, see [sdk.md](sdk.md).

## Responsibilities

| Component | Provides |
|---|---|
| Observer | Built-in activation `auth-start`, revision checks, short-lived browser sessions and bridge, public authentication projection, activation state and waiting Run updates |
| Slave HTTP runtime | Isolated activation container, selected credential mount, optional state volume, private authentication requests and health reports |
| SDK `loom-auth` | One portal for selected activation URIs, IdP-partitioned browser profiles, callback/cookie forwarding, polling and explicit reauthentication |
| Capability gateway | Provider login/exchange or credential plugin, credential storage/refresh, real access verification, expiry detection, private hooks below |

The system does not implement an arbitrary provider's OAuth exchange or acquire
service credentials automatically. Keep provider logic in the gateway. Do not
ship a separate user-side login script, export auth hooks as business actions,
or put credentials into a Run. `auth-start` is offered by Observer, not declared
in `action_exports`. External clients use Observer; only the runtime calls the
gateway's private HTTP paths.

## Choose and declare an interaction

| `interaction.mode` | Gateway responsibility | `browser_path` |
| `authentication.idp` | Optional stable HTTPS IdP origin used by the local helper to share a browser profile across activations | None |
|---|---|---|
| `redirect` | Open provider authorization; accept the fixed local callback and finish the provider exchange, including provider state/PKCE checks where applicable | Required |
| `browser-cookie` | Open provider login at its original site; accept and verify the named session cookie | Required; `interaction.cookie_name` also required |
| `device-code` | Start and poll device authorization through the provider/plugin; expose its verification URL/code | Omit |
| `credential-profile` | Load operator-supplied credentials and verify access; no browser grant | Omit |

For an unauthenticated service, omit `body.authentication`. A device-code
gateway can still need a credential profile for kubeconfig/provider settings;
the interaction mode and activation credential selection are separate concepts.

Example **fragment inside the package `body`**, for a cookie-based service:

```json
{
  "authentication": {
    "start_path": "/v1/auth/start",
    "status_path": "/v1/auth/status",
    "browser_path": "/v1/auth/browser",
    "idp": "https://login.example/tenant",
    "interaction": {
      "mode": "browser-cookie",
      "cookie_name": "service_session"
    }
  },
  "persistent_state": true
}
```

For redirect, use `"interaction": {"mode": "redirect"}`. Add a stable HTTPS
`authentication.idp` origin when activations using this package authenticate against the same
identity provider; the local helper then reuses its saved browser profile. For device-code or
credential-profile, set that mode and remove `browser_path`. Paths must be
distinct origin-relative paths without query/fragment, and must not overlap the
health or business paths. Complete the normal image, port, health and business
export declarations. Use an operator-supported egress network when needed.

New publication rejects legacy `authentication.client` and package-level
`body.credential_profile`. Historical read/digest compatibility is not a way
to publish a new package. Names and immutable references come from Observer;
transport URLs and private HTTP paths retain their transport syntax.

## Private HTTP hooks

`GET health_path` checks that the gateway itself can serve requests, including
while waiting for login. A successful health response alone does not establish
authenticated readiness. The following hooks exchange plain JSON, not the
public Observer `{resource, revision, authentication}` envelope.

### Start and status

- `POST start_path` receives `{}` during activation. Non-browser `auth-start`
  also sends `{}`. Browser methods must accept this initial probe without
  requiring or inventing a browser session; report current authentication need.
- Explicit browser `auth-start` sends the Observer-generated context below.
  Validate it, replace the previous challenge, and return `auth_required`.
  Observer currently requires this pending result when creating a browser session.
- `POST status_path` receives `{}` on periodic health checks. Verify credentials
  and report current status; do not restart login on every status poll.

Browser start context (illustrative session ID and timestamp):

```json
{
  "session_id": "abcdefghijklmnopqrstuvwxyzABCDEF",
  "callback_uri": "http://localhost:3000/login-callback",
  "expires_at": 1792583100
}
```

Use the received values, not these examples. Session IDs match
`[A-Za-z0-9_-]{32,128}`. `expires_at` is finite Unix time in seconds, about five
minutes ahead; reject expired contexts. The callback URI is fixed by the current
protocol. It is not a configurable provider URL or the gateway's public address.

Return a 2xx JSON object with one of these statuses:

| Hook `status` | Meaning | Healthy running activation |
|---|---|---|
| `authenticated` | Required upstream access has been verified | `ready` |
| `auth_required` | Missing/expired credential or pending human login | `pending_auth` |
| `auth_failed` | Verification/exchange/connection failed | `degraded` |

For `auth_required`, provide available `verification_uri`, `user_code`,
`expires_at` and `poll_after_seconds`. Device-code URLs/codes come from the
provider, and the gateway must keep the grant poller alive; do not return a
challenge from a dead process. These fields may be public. Arbitrary debug,
credential and provider response fields are not part of the public projection;
do not rely on extra fields such as `retryable` or `qr_payload` reaching the SDK.
Non-2xx, invalid JSON or an unknown status is treated as `auth_failed`.

### Browser messages

`POST browser_path` handles only these two messages:

```json
{"action": "open", "session_id": "abcdefghijklmnopqrstuvwxyzABCDEF"}
```

Return exactly `{"status": "redirect", "location": "https://provider.example/login"}`
using the real provider URL. It must be HTTPS without embedded credentials.
For redirect mode, preserve the provider's required callback/state flow. For
browser-cookie mode, preserve the original site's callback; the SDK reads the
declared cookie from its own browser context at the opened site's origin.

```json
{
  "action": "callback",
  "session_id": "abcdefghijklmnopqrstuvwxyzABCDEF",
  "query": {"code": ["provider-code"], "state": ["provider-state"]}
}
```

For redirect, `query` contains callback parameters as arrays of strings. For
browser-cookie it instead contains exactly the declared cookie name and one
value, e.g. `{"service_session": ["private-cookie-value"]}`. Cookie values must
fit the current 4096-character callback limit. Validate the matching session,
expiry and one-time `created → bound → consumed` sequence; reject stale/replayed
messages. Complete the provider exchange/verification, store credentials
privately and return exactly `{"status": "accepted"}` on success (HTTP 200).
Readiness still depends on `status_path`; callback acceptance is not proof of
usable business access. Observer bridge tokens/bindings are managed by the
system and are not fields the gateway should add to this protocol.

## Instance configuration and credential lifetime

- `activate-package` selects `credential_profile` alongside `instance_config`.
  The operator prepares it; the gateway reads `/run/loom/credentials` read-only.
  Omission/null preserves an existing binding and mounts none for a new instance.
  Changing the profile selection requires a new instance name. Credential files
  are never uploaded as content or pasted into activation inputs.
- Non-secret `instance_config` fields and `target_resource_ref` are mounted in
  `/run/loom/instance.json` when configured. They are flattened into that JSON,
  not nested under `instance_config`. Public attributes are separately governed
  by MCUE; do not automatically copy this file into resource descriptions.
- With `body.persistent_state=true`, `/var/lib/loom/service` is writable state
  isolated by workspace, Slave, package digest and instance. Persist refreshed
  credentials there with restrictive permissions; do not write into the
  read-only credential profile or use a shared global login cache. A package
  version upgrade does not automatically inherit another digest's state volume.
- Define precedence if a gateway supports both mounted and interactive
  credentials. Reject conflicting sources rather than silently changing the
  effective account. Grafana's current implementation rejects interactive login
  when mounted profile credentials exist.

Detect real credential expiry through a bounded upstream access check. A token
file's presence, an HTTP 200 login page or a subprocess exit code alone is not
enough. Use the provider's refresh mechanism where available; missing/expired
credentials require human login again, while network/host-key failures are not
proof that a user needs to log in. Periodic background revalidation may retain a
previous verified success only for a bounded interval; a new challenge, failure
or timeout must end that grace period. Explicit auth-start clears stale attempts
and cached success before checking the new attempt.

Distinguish normal token rotation from actual credential expiry. For example,
Grafana 13.2.4 returns `401` with `messageId=session.token.rotate`; the Grafana
v4 gateway performs the fixed same-origin `POST /api/user/auth-tokens/rotate`,
persists the updated cookie and retries the original read once under its
instance lock. Plain/revoked-session 401s still require login, while renewal
network/service failures are auth_failed. Do not turn every HTTP 401 into a QR
prompt or confuse a rotation interval with the lifetime of a user's login.

Reauthentication uses the same activation/resource URI and current revision.
Observer invalidates the previous browser challenge, rejects stopped/stale
activations, and allows explicit auth-start to recover an eligible failed
activation. Ordinary health reports cannot clear a failed lifecycle state.
Do not recreate an activation or resubmit an `awaiting_auth` Run just to log in;
health updates resume the waiting Run. Challenge expiry and credential expiry
are different deadlines.

## Build, publish and verify

1. Implement the selected hooks and test the provider adapter. Keep credentials
   and raw auth responses out of logs, business results and public descriptions.
2. Build the gateway image and obtain its immutable image reference. Publish
   schemas, descriptors, contracts and manifest with `ServicePackage.publish`
   as described in [sdk.md](sdk.md). Use a new package version for a changed
   immutable package; update dependencies that reference that version.
3. Through Observer, activate a named instance with its configuration/profile
   and optional target resource. Use the authoritative returned activation URI.
   DESCRIBE it and invoke its offered `auth-start` with `expected_revision` from
   `metadata.revision`, normally via `loom-auth`. No custom client/session file
   is needed. Poll ready/authenticated before invoking a representative business
   action through Observer.
4. Validate the relevant failure paths: missing/expired credentials; successful
   login followed by a real business read; challenge timeout and reauthentication
   on the same activation; failed upstream verification; independent credentials
   for two instances. For browser adapters also exercise expired, duplicate and
   replaced session callbacks. Confirm DESCRIBE omits private bridge context and
   credentials. For persistent state, verify restart and provider refresh.

In a Loom source checkout, `capability_services/suanli_vm_ssh_http` demonstrates
redirect, `grafana_http` browser-cookie, `kubernetes_http` device-code, and
`vm_ssh_http`, `outline_http`, `suanlicloud_http` credential-profile. Reuse the
matching contract and tests, not their provider-specific login assumptions.
If a provider cannot fit these interactions, extend the protocol/runtime/SDK
explicitly before promising support in a package declaration.

## Service interaction and restart recovery

A package authentication hook remains private regardless of its public data
binding. Declare `runtime_binding.interaction` per export; default task creates
a Run, request-reply returns business JSON, byte-stream opens a channel. Follow
[service-bindings.md](service-bindings.md) for endpoint trust and dependency grants.
Do not put service access tokens in instance_config or package manifests.

For persistent authentication, set `body.persistent_state=true` and keep the
upstream client's credential/refresh cache under `/var/lib/loom/service`. A
same-version restart reuses that state; a different package digest gets its own
volume and requires its own authorization. On authentication expiry, report
`auth_required`; repeated `auth-start` must initiate a new flow on the same
activation and resume new requests after authorization. Network errors and RBAC
failures must not be presented as login requests. Supervised streams abort old
requests and establish only new channels after recovery.
