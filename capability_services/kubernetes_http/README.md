# Kubernetes HTTP capability service

Build the image and record its immutable digest before creating the package:

```bash
docker build -t registry.example/loom/kubernetes-gateway:1 capability_services/kubernetes_http
docker push registry.example/loom/kubernetes-gateway:1
docker inspect --format '{{index .RepoDigests 0}}' registry.example/loom/kubernetes-gateway:1
```

Use the resulting `registry/...@sha256:...` in `package.example.json`, upload the
referenced operation descriptors and IoContracts through Observer, then register
the completed manifest as a workspace-reusable package. The example intentionally
contains placeholder refs and is not a production package until those refs and
the image digest are replaced.

The Slave operator must configure the matching profile directory before
activation. The gateway does not read the host user's kubeconfig and does not
expose a generic command endpoint. Since Loom's default internal network has no
egress, configure an operator-owned bridge network and set
`LOOM_RUNTIME_PLUGIN_EGRESS_NETWORK`; the package's `network_profile: egress`
selects that configured network without accepting an arbitrary Docker network.
