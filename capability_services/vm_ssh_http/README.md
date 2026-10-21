# Ordinary VM SSH capability service

Public resource use follows [the shared GRIP workflow](../README.md): publish the
artifact directory with `scripts/publish_service_package.py`, activate the package
on a Slave, then invoke the ready activation's discovered action URI. The HTTP
paths below describe the private runtime-to-container binding.

This package exposes one fixed `container:http/1` capability, `/v1/exec`, for
an ordinary SSH-accessible VM. The activation-owned `/run/loom/instance.json`
provides the host, user, and port. The request can provide only a shell command
and timeout; it cannot select a target or pass SSH options.

The v4 execution contract accepts up to 1,048,576 command characters and an
8 MiB JSON request. The command is sent on SSH stdin to `sh -s`. The default
timeout is 300 seconds; callers may request 1–3600 seconds. Each output stream
retains at most 16 MiB, with `truncated=true` when bytes are discarded. See the
[shared execution limits and build commands](../README.md#ssh-执行限制).

The activation mounts an SSH private key at the credential profile's
`id_ed25519` path. The service keeps `known_hosts` in its persistent state
volume, uses `StrictHostKeyChecking=accept-new` on first contact, and rejects a
changed host key. Authentication status is exposed through the standard
`/v1/auth/status` path; browser or provider login is intentionally unsupported.

Choose the profile in the GRIP `activate-package` input, for example
`"credential_profile": "vm-ssh-default"`, alongside
`"instance_config": {"host": "vm.example.com", "port": 22, "user": "worker"}`.
The Slave operator prepares that profile directory with `id_ed25519` before
activation. The package manifest contains no credential selection. Different
instances of the same package can use different SSH keys; neither the profile
name nor the connection configuration appears in public resource descriptions.

The descriptor carries `schemas/instance-config.json`. Publication includes
`validators/exec-success.py`: successful execution requires exit code zero and
no timeout. The activation offers GRIP `auth-start` for the configured SSH key
profile; it does not perform an interactive browser login.
