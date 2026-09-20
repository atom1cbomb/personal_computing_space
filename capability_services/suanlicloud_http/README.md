# Suanli Cloud HTTP capability package

The gateway implements the `SUANLI-HMAC-SHA256` protocol documented in
`/root/cloud-backend/docs/api/signing.md`. The provider source defines the wire
route as `POST /v1/{service}` with `Action` in the JSON body; the OpenAPI query
form (`?Action=...`) is only a documentation key and is not sent by this image.

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
branch is added to Observer, Driver, or Slave core. The signer reproduces the
reference vector in `cloud-backend/internal/auth/testdata/vectors.json`.

The production endpoint now runs cloud-backend revision `6dd51a0`, which fixes
the HTTP verifier's Host canonicalization. Image digest
`sha256:7c39d0c7d84dbb4fceea76d514c3c9c3b6bfb03979d53c7fc43327af6552e888` was
published as package `suanlicloud-http/v2` and activated on production
`slave-a`. Standard signed requests from the running container have been
verified against production for all six read-only operations.
