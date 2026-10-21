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
and uploads them. Package v4 exports six business actions. Authentication is
activation lifecycle metadata, invoked publicly through GRIP `auth-start`.
The public `vcjobs` resource selector resolves to `jobs.batch.volcano.sh`;
`jobs` continues to select native Kubernetes Jobs.

The Slave operator must configure the matching profile directory before
activation. The gateway does not read the host user's kubeconfig and does not
expose a generic command endpoint. Since Loom's default internal network has no
egress, configure an operator-owned bridge network and set
`LOOM_RUNTIME_PLUGIN_EGRESS_NETWORK`; the package's `network_profile: egress`
selects that configured network without accepting an arbitrary Docker network.

Select credentials in the activation input, alongside `instance_config`, not
in the package manifest:

```python
activation = await client.invoke(slave_uri, client.names.action("activate-package"), {
    "package": package_uri,
    "instance_name": "beijing",
    "credential_profile": "k8s-bj",
    "idempotency_key": "activate-k8s-beijing",
})
```

Another instance, `nanjing`, can use `credential_profile: k8s-nj` with the same
package URI. Each profile supplies its own `kubeconfig` and `policy.json`; its
context and namespace select the cluster and allowed scope, including Volcano
jobs in that cluster. The runtime mounts the selected directory read-only at
`/run/loom/credentials`. Authentication state is isolated per instance. A retry
or restart preserves the selection; changing profiles requires a new instance
name. The profile name and files are not returned by resource DESCRIBE.

Choose `KUBECTL_VERSION` at image build time to match the target cluster's
supported client version range. The image currently pins kubelogin 1.30.1,
which returns `client.authentication.k8s.io/v1beta1` credentials; the selected
kubeconfig's `users[].user.exec.apiVersion` must use that same value. A `v1`
configuration with this plugin can complete browser login but fail the access
probe because the returned credential API version differs. After correcting a
profile for an activation already marked `failed`, invoke `activate-package`
with the same package, instance and profile and a new idempotency key to retry.
The resource URI is retained and its revision advances; use the returned revision
for subsequent calls.

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
