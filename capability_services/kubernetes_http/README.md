# Kubernetes HTTP capability service

Public resource use follows [the shared GRIP workflow](../README.md): publish the
artifact directory with `scripts/publish_service_package.py`, activate the package
on a Slave, then invoke the ready activation's discovered action URI. The HTTP
paths below describe the private runtime-to-container binding.

Build the image and record its immutable digest before creating the package:

```bash
docker build -t registry.example/loom/kubernetes-gateway:1 capability_services/kubernetes_http
docker push registry.example/loom/kubernetes-gateway:1
docker inspect --format '{{index .RepoDigests 0}}' registry.example/loom/kubernetes-gateway:1
```

Pass the resulting immutable image ref to the shared publisher. Descriptor sources,
schemas and IoContracts are included in this directory; the publisher resolves
and uploads them. Package v2 exports six business actions. Authentication is
activation lifecycle metadata, invoked publicly through GRIP `auth-start`.

The Slave operator must configure the matching profile directory before
activation. The gateway does not read the host user's kubeconfig and does not
expose a generic command endpoint. Since Loom's default internal network has no
egress, configure an operator-owned bridge network and set
`LOOM_RUNTIME_PLUGIN_EGRESS_NETWORK`; the package's `network_profile: egress`
selects that configured network without accepting an arbitrary Docker network.

The action schemas and content-addressed IoContracts live in `schemas/` and
`contracts/`. Regenerate their refs after changing an action boundary with:

```bash
python scripts/generate_service_contracts.py
python scripts/generate_service_contracts.py --check
```

These contracts intentionally leave `success_semantics` null: a 2xx JSON
response that matches the action output schema is the gateway's success
criterion. A future domain-specific criterion must add a deterministic
`success_validator_ref`; adding opaque semantics alone would require
attestation.
