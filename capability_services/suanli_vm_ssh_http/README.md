# Suanli VM SSH-over-HTTP capability package

Public resource use follows [the shared GRIP workflow](../README.md): publish the
artifact directory with `scripts/publish_service_package.py`, activate the package
on a Slave, then invoke the ready activation's discovered action URI. The HTTP
paths below describe the private runtime-to-container binding.

This is one ordinary `service + container:http/1` package. The runtime mounts
the activation-specific `/run/loom/instance.json` and persistent
`/var/lib/loom/service` volume. The service exposes only `GET /healthz`, the
generic authentication paths, and `POST /v1/exec`.

`/v1/exec` accepts `{command, timeout_seconds}`. The VM host, user and port
are never accepted from a request; they come from the activation config.
The v3 execution contract accepts up to 1,048,576 command characters and an
8 MiB JSON request. The script is sent on SSH stdin to `sh -s`; the default
timeout is 300 seconds and the accepted range is 1–3600 seconds. Each output
stream retains at most 16 MiB with `truncated=true` on overflow. See the
[shared execution limits and build commands](../README.md#ssh-执行限制).
OpenSSH uses `StrictHostKeyChecking=accept-new`, a per-instance
`known_hosts`, and a stable resource-derived `HostKeyAlias` (TOFU on first
connection, strict thereafter). A changed key fails the command.

The container starts `loom-opkssh`, a small renewal worker using pinned official
OpenPubkey 0.23.0 and opkssh 0.14.0 libraries, with the configured
Suanli provider. The runtime supplies the one fixed Observer callback URI for
the short-lived browser session; callers cannot override it with environment
variables or request fields. The exact localhost callback is already
registered with the OIDC client. The package does not invent a device-code
protocol.

Use the [unified SDK login portal](../../docs/authentication.md) with
`suanli-vm-ssh-http/v5`. The package declares `interaction.mode=redirect`.
The client opens an isolated browser, captures the registered localhost callback,
and forwards it to the Observer's one-time bridge. The worker stays alive after
login and refreshes one minute before ID-token expiry. It verifies the renewed
identity and rebuilds the SSH certificate using the official opkssh code.
Private state, including rotating refresh tokens, is atomically saved with 0600
permissions in the activation volume before publishing the new certificate.
Restart resumes this state without a browser. A provider-config change cannot
reuse an unrelated checkpoint.

Transient renewal failures retain credentials and retry with 2–60 second
backoff. Explicit OAuth `invalid_grant` requires login; network and host-key
errors remain `auth_failed`. Readiness always includes a real SSH access check.
An explicit new login stops the old worker and removes its credentials and
challenge before starting another attempt. Reuse the same activation to retry.

The SSH server must permit refreshed ID tokens (`oidc_refreshed` in its opkssh
provider policy). A fixed `24h`/`oidc` policy still enforces its original login
deadline; client refresh cannot change that policy. This package does not modify
the VM's SSH authorization policy. The IdP must issue a usable refresh token.
Plain SSH-key capability `vm-ssh-http` is unrelated and remains unchanged.

The package is now v5. Its new digest has a separate credential volume, so the
first activation needs its own login; subsequent same-version restarts resume
automatically. The `suanlicloud-http/v6` creation descriptor selects this version.

Run the worker tests with `cd capability_services/suanli_vm_ssh_http/renewal &&
go test -race ./...`; Python gateway lifecycle tests are in
`tests/capability_services/test_suanli_vm_ssh_gateway.py`. The Dockerfile builds
the worker in a separate Go stage and includes only the binary in the runtime.

Build and publish an immutable digest image, then replace the placeholder in
`package.example.json`.

Upload `validators/exec-success.py` with the action contract. It checks the
declared success semantics (`exit_code == 0` and `timed_out == false`) rather
than treating any HTTP 200 response as successful SSH execution.

The action descriptor source in `descriptors/exec.json` declares the
`compute.vm.remote_shell` capability kind. A Suanli Cloud resource effect may
select this package by capability kind, bind its activation to the created VM,
and require the activation to reach `authenticated` before invoking `/v1/exec`.

## Activation 配置契约

发布 `descriptors/exec.json` 前先上传 `schemas/instance-config.json`，保留返回的
不可变 `instance_config_schema_ref`。配置只接受 `host`、`port`、`user`；认证凭据
仍由 capability 认证协议管理，不放入配置或 ContentStore。
