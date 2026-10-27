# Suanli Cloud HTTP capability package

Public resource use follows [the shared GRIP workflow](../README.md): publish the
artifact directory with `scripts/publish_service_package.py`, activate the package
on a Slave, then invoke the ready activation's discovered action URI. The HTTP
paths below describe the private runtime-to-container binding.

`activate-package` returns the activation resource URI automatically. DESCRIBE
that URI to read all 12 business actions in `resource.metadata.actions`, including
their descriptions, input/output schema refs and availability. `offers` contains
the currently callable business actions plus `stop` and, while running,
`auth-start`. Pending or stopped instances retain action descriptions. Starting
the container manually does not register an activation with Observer.

Business contracts use `action.v1`, `action_exports`, `action_descriptor_ref`
and `action_ref`. A GRIP business call returns `{run: <Run URI>}`; its eventual
gateway result uses `{action, result}`.

The gateway implements the `SUANLI-HMAC-SHA256` protocol documented in
`/root/cloud-backend/docs/api/signing.md`. The provider source defines the wire
route as `POST /v1/{service}` with `Action` in the JSON body; the OpenAPI query
form (`?Action=...`) is only a documentation key and is not sent by this image.

This service exposes a fixed, named subset of the Suanli Cloud control-plane
OpenAPI rather than a generic URL or `Action` proxy. It supports read-only
region/zone/node/instance/image queries and explicit instance lifecycle
actions. The image reads `AccessKeyId` and `SecretKey` from the mounted
operator credential profile; those files are never part of the package,
Observer content, or run output.

Build an immutable image and replace the digest in `package.example.json`:

```bash
docker build -t localhost:5000/loom/suanlicloud-gateway:1 capability_services/suanlicloud_http
docker push localhost:5000/loom/suanlicloud-gateway:1
docker inspect --format '{{index .RepoDigests 0}}' localhost:5000/loom/suanlicloud-gateway:1
```

The profile directory must contain the operator-managed credential
files `AccessKeyId` and `SecretKey`. Configure it outside Git as
`/var/lib/loom/runtime-credentials/suanlicloud-prod/` before activation.
Signing retains RFC3339 fractional seconds: identical requests within the same
second must not reuse a signature rejected by the provider's replay protection.
The directory must be traversable by container UID 65534 and the files must
be readable by it (directory owner 65534, mode 0700; files owner 65534, mode 0600).

The provider's AK/SK signer is isolated in `server.py`; no Suanli-specific
branch is added to Observer, Driver, or Slave core. The signer reproduces the
reference vector in `cloud-backend/internal/auth/testdata/vectors.json`.

Historical validation before the GRIP migration: the production endpoint ran cloud-backend revision `6dd51a0`, which fixes
the HTTP verifier's Host canonicalization. Image digest
`sha256:7c39d0c7d84dbb4fceea76d514c3c9c3b6bfb03979d53c7fc43327af6552e888` was
published as package `suanlicloud-http/v2` and activated on production
`slave-a`. Standard signed requests from the running container have been
verified against production for all six read-only actions. This historical
evidence does not verify the new GRIP publication or invocation paths.

The current publication template is `suanlicloud-http/v6`. Its `instances-run`
resource effect declares `mcue_ref` for `computing/vm/v1`; `ServicePackage.publish`
resolves that definition in the target workspace. Register returned VM identities
with this definition, then use the declared SSH access package and cleanup action.
The descriptor requires the MCUE Observer/SDK release. The v5 template also
requires activation credential selection: pass
`"credential_profile": "suanlicloud-prod"` in the GRIP `activate-package` input.
There is no profile selection in the package manifest. Different named
instances can select different operator profiles and cloud accounts without
republishing the package. The selected profile remains bound across retries
and restarts; use a new instance name when selecting another profile.

The exported action boundaries are recorded under `schemas/` and
`contracts/`. They describe the gateway's `{action, result}` response
envelope and the required OpenAPI request fields without copying the entire
provider OpenAPI document. Regenerate and verify the content refs with:

```bash
python scripts/generate_service_contracts.py
python scripts/generate_service_contracts.py --check
```

The IoContracts intentionally use `success_semantics: null`: provider HTTP/API
errors are rejected by the gateway/runtime, and successful responses are
checked by the action output schemas. A future criterion about actual VM
state must be paired with a deterministic validator; semantics alone would
correctly enter attestation instead of being guessed as success.

The action descriptor sources under `descriptors/` are part of the public
package contract. In particular, `descriptors/instances-run.json` declares
that a created VM is not ready for work until `instances-list` reports a
matching instance with `State=running` and an address (`instances-status`
does not return IP addresses), and that remote execution uses the
`suanli-vm-ssh-http` package after authentication. Publish these descriptors
through the Observer content data plane and use the returned descriptor refs in
the package manifest; the Observer exposes verified descriptor payloads to
external agents through capability-package discovery.

Publish `suanli-vm-ssh-http/v5` first, then pass its returned GRIP URI through
the publisher dependency map as described in the shared guide.
