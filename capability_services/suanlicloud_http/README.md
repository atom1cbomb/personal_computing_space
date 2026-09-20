# Suanli Cloud HTTP capability package

**Draft — not ready for use.** The supplied OpenAPI specifies
`AKSKSignature` but omits its signing protocol. `_auth_headers` deliberately
fails with `provider_signature_spec_required` until an official SDK or signing
example is supplied. Production `suanlicloud-http/v1` was tried and then
stopped; it is not an authenticated/usable package.

This service exposes a fixed, named subset of the Suanli Cloud control-plane
OpenAPI rather than a generic URL or `Action` proxy. It supports read-only
region/zone/node/instance/image queries and explicit instance lifecycle
operations. The image reads `AccessKeyId` and `SecretKey` from the mounted
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
The directory must be traversable by container UID 65534 and the files must
be readable by it (directory owner 65534, mode 0700; files owner 65534, mode 0600).

The provider's AK/SK signer is isolated in `server.py`; no Suanli-specific
branch is added to Observer, Driver, or Slave core.
