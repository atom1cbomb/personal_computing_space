# Suanli VM SSH-over-HTTP capability package

This is one ordinary `service + container:http/1` package. The runtime mounts
the activation-specific `/run/loom/instance.json` and persistent
`/var/lib/loom/service` volume. The service exposes only `GET /healthz`, the
generic authentication paths, and `POST /v1/exec`.

`/v1/exec` accepts `{command, timeout_seconds}`. The VM host, user and port
are never accepted from a request; they come from the activation config.
OpenSSH uses `StrictHostKeyChecking=accept-new`, a per-instance
`known_hosts`, and a stable resource-derived `HostKeyAlias` (TOFU on first
connection, strict thereafter). A changed key fails the command.

The container starts the official `opkssh login` client with the configured
Suanli provider. The runtime supplies the one fixed Observer callback URI for
the short-lived browser session; callers cannot override it with environment
variables or request fields. The exact localhost callback is already
registered with the OIDC client. The package does not invent a device-code
protocol.

The package also carries `client/loom_vm_auth.py`, a user-side local browser
bridge. It listens only on the user's `127.0.0.1:3000`, forwards only `/login`
and `/login-callback` to the one-time HTTPS Observer bridge URL, and exits when
the user stops it. It does not open an SSH tunnel, require an Observer host
account, or proxy arbitrary URLs. The `auth/start` response supplies the
short-lived session token and bridge URL; never copy that token into a
permanent configuration file.

Build and publish an immutable digest image, then replace the placeholder in
`package.example.json`.
