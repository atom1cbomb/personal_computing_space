# Unified authentication

For creating a capability package, read
[authentication-authoring.md](authentication-authoring.md) for the service-side
contract and implementation workflow. This reference covers using a package.

Requires SDK 0.7, the matching Observer/HTTP runtime, and a package declaring
authentication.interaction. Current packages: suanli-vm-ssh-http/v5 (redirect),
kubernetes-http/v6 (device-code), grafana-http/v4 (browser-cookie, automatic token
rotation). Grafana v3 supports unified login but needs the gateway upgrade to
handle normal short-lived token rotation. Discover actual
activation URIs; installing a client does not upgrade deployed packages.

Install the bundled SDK wheel with its auth extra, then run
`python -m playwright install chromium` on the user's browser machine. Start:

```sh
loom-auth --observer https://observer.example --token-file /private/loom-token \
  --activation '<Observer-returned activation URI>'
```

Repeat --activation for multiple instances. Add --allow-http only for an HTTP
Observer. Open http://localhost:3000/. The client obtains the latest revision,
invokes auth-start, opens an isolated browser using the declared method, and
polls that activation until ready/authenticated. The user completes the original
provider login. Do not prepare package-specific Python helpers or session files.

For expiry, closed windows or failed authentication, use the same portal's
reauthentication button. Do not recreate the activation or resubmit a waiting
business Run just to log in. A new auth-start invalidates the old browser challenge;
the five-minute challenge expiry is distinct from credential expiry. Different
activations keep independent credentials. Credentials and bridge tokens never
belong in descriptions, content uploads, business inputs, logs or chat.

Programmatic auth-start returns resource, revision and authentication. The latter
contains status/method and available verification_uri, user_code, expires_at,
poll_after_seconds; browser methods also return a private client bridge context.
DESCRIBE metadata.authentication omits that private context. Poll the current
activation; receiving a callback alone does not mean ready. If an activation is
stopped or its revision changes, obtain a new description before proceeding.

For operator-managed Grafana credential profiles, update the mounted credential
through the operator workflow and wait for health verification; do not start
interactive cookie login on the same profile-bound instance.

Suanli SSH v5 persists its OpenPubkey refresh state and resumes renewal after
container restart; the VM must allow `oidc_refreshed` certificates. Fixed SSH
login deadlines and revoked refresh tokens still require reauthentication.
AutoPaper v2 keeps browser-cookie login and REST actions. Its backend refreshes
OAuth tokens; the accompanying backend fix preserves sessions on temporary
refresh errors and serializes rotation. Upgrading only the gateway is insufficient
when the backend still deletes sessions on network failures.
