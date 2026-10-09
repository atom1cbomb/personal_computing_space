# Ordinary VM SSH capability service

This package exposes one fixed `container:http/1` capability, `/v1/exec`, for
an ordinary SSH-accessible VM. The activation-owned `/run/loom/instance.json`
provides the host, user, and port. The request can provide only a shell command
and timeout; it cannot select a target or pass SSH options.

The activation mounts an SSH private key at the credential profile's
`id_ed25519` path. The service keeps `known_hosts` in its persistent state
volume, uses `StrictHostKeyChecking=accept-new` on first contact, and rejects a
changed host key. Authentication status is exposed through the standard
`/v1/auth/status` path; browser or provider login is intentionally unsupported.
