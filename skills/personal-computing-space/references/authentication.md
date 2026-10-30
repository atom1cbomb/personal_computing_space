# Unified authentication

For creating a capability package, read
[authentication-authoring.md](authentication-authoring.md) for the service-side
contract and implementation workflow. This reference covers using a package.

Requires the matching SDK/Observer runtime and a package declaring
`authentication.interaction`. The package descriptor is authoritative for the
interaction mode, browser path, status fields, credential profile and refresh
behavior. Discover the deployed package and activation URIs; installing a
client does not upgrade deployed packages.

Install the bundled SDK wheel with its auth extra, then run
`python -m playwright install chromium` on the user's browser machine. Start:

```sh
loom-auth --observer https://observer.example --token-file /private/loom-token \
  --activation '<Observer-returned activation URI>'
```

Repeat --activation for multiple instances. Add --allow-http only for an HTTP
Observer. Open http://localhost:3000/. The client obtains the latest revision,
invokes auth-start, opens a browser using the declared method, and polls that
activation until ready/authenticated. SDK 0.9.3 uses one private browser profile
for the selected resources. Chromium follows the actual login redirects and enforces
cookie and origin boundaries; packages do not need to declare an IdP. Session cookies
are saved privately across browser restarts. Provider changes follow the new login
flow automatically. Resource authorization/consent may still require confirmation.
Use `--auth-state-dir` to separate accounts or `--no-auth-state` for independent,
temporary contexts. Old per-IdP profiles are retained but not merged; the first login
after upgrading establishes the new shared profile. Do not prepare package-specific
Python helpers or session files.

For expiry, closed windows or failed authentication, use the same portal's
reauthentication button. Do not recreate the activation or resubmit a waiting
business Run just to log in. A new auth-start invalidates the old browser challenge;
the five-minute challenge expiry is distinct from credential expiry. Queued logins
start their challenges only after obtaining the shared browser.
Use one running helper per profile directory; an unavailable profile is reported
explicitly. A rejected service cookie does not clear unrelated provider sessions.
Credentials and bridge tokens never
belong in descriptions, content uploads, business inputs, logs or chat.

Programmatic auth-start returns resource, revision and authentication. The latter
contains status/method and available verification_uri, user_code, expires_at,
poll_after_seconds; browser methods also return a private client bridge context.
DESCRIBE metadata.authentication omits that private context. Poll the current
activation; receiving a callback alone does not mean ready. If an activation is
stopped or its revision changes, obtain a new description before proceeding.

For operator-managed credential profiles, update the mounted credential through
the operator workflow and wait for health verification; do not start an
interactive login on the same profile-bound instance. Package-specific refresh
and restart behavior must be read from that package's descriptor and health
contract.
