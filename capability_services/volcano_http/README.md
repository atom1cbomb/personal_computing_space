# Volcano Dashboard capability package

`volcano-http/v2` reads a Volcano Dashboard's own JSON API. Each activation
selects a Dashboard origin, preserving its HTTPS port. It is separate from
`kubernetes-http`, which uses a kubeconfig to operate Kubernetes and submit jobs.
The public entry point is Observer GRIP; HTTP paths below are private bindings.

| action | Private route | Arguments |
|---|---|---|
| namespaces_list | /v1/namespaces/list | none |
| queues_list | /v1/queues/list | optional state, search |
| podgroups_list | /v1/podgroups/list | optional namespace, status, search |
| pods_list | /v1/pods/list | optional namespace, status, search |
| jobs_list | /v1/jobs/list | optional namespace, status, search, queue |
| gpu_usage | /v1/gpu/usage | optional namespace, resource_name |

Namespace defaults to `all`, translated to the Dashboard's `All` filter. List
results contain `items`, `total_count`, and `complete=true`; the gateway follows
upstream pagination and rejects an incomplete result. Every export is SafeRead.

Current Dashboard read APIs return JSON without a browser session, so this v2
package requires no login or credential profile. A future 401, 403, login redirect,
or HTML login response is an explicit error. The documented browser-login port
workaround is not needed to call these currently public JSON endpoints.

## Instance configuration and resource description

The activation's only service configuration is:

```json
{"api_origin":"https://volcano-prod.bj.cs.ac.cn"}
```

The generic runtime mounts it at `/run/loom/instance.json`, alongside the
runtime-owned `target_resource_ref`. The service checks an HTTPS origin without
credentials, path, query, or fragment. Call arguments cannot select another URL.
This requires the HTTP runtime revision that transports service-owned JSON;
older runtimes incorrectly require host/user/port in every instance config.

Register a `computing/volcano` resource and bind the activation's `target` to it.
The MCUE definition is [computing-volcano-v1.json](../../loom_v2/mcue_types/computing-volcano-v1.json).
DESCRIBE exposes `metadata.attributes.dashboard_url` and
`metadata.attributes.kubernetes_ref`, as well as ready activation bindings.
The Kubernetes reference is the exact existing Kubernetes activation URI returned
by Observer. It is a navigation link; updating a public description does not
change an activation's connection settings.

The public `dashboard_url` can differ from the runtime `api_origin`. In this
production network, split DNS sends Beijing and Beijing ICT to internal Dashboard
ingresses on port 443; their public browser URLs retain ports 7443 and 10062.
Nanjing uses port 10062 from both networks. The gateway makes no automatic port
substitutions: these are explicit per-activation configuration choices.

| Instance | Dashboard origin |
|---|---|
| beijing | https://volcano-prod.bj.cs.ac.cn:7443 |
| nanjing | https://volcano-prod.nj.cs.ac.cn:10062 |
| beijing-ict | https://volcano-prod.ictbj.cs.ac.cn:10062 |

Build and publish using the [shared workflow](../README.md), then connect:

```sh
docker build -t registry.example/loom/volcano-gateway:1 capability_services/volcano_http
docker push registry.example/loom/volcano-gateway:1
# Publish with the immutable registry/repository@sha256:digest, then retain
# the package URI, Slave URI and Kubernetes activation URI returned by Observer.
python scripts/connect_volcano.py \
  --observer "$LOOM_OBSERVER_URL" --allow-http \
  --package "$VOLCANO_PACKAGE_URI" --slave "$SLAVE_URI" \
  --name beijing --display-name '北京 Volcano' \
  --dashboard-url https://volcano-prod.bj.cs.ac.cn:7443 \
  --api-origin https://volcano-prod.bj.cs.ac.cn \
  --kubernetes-ref "$KUBERNETES_ACTIVATION_URI"
```

The authoring helper reads `LOOM_GRIP_TOKEN`, publishes the MCUE through GRIP,
checks the supplied Kubernetes activation, registers or updates the named resource,
and activates the package on the selected Slave. It returns Observer's resource
and activation URIs. Different instances have independent addresses and bindings.
If `--api-origin` is omitted, the helper uses the supplied public origin.
The original registration's `metadata.package` remains its immutable provenance;
current callable actions are selected through the ready activation binding.

## GPU allocation meaning

`gpu_usage` defaults to `resource_name="nvidia.com/gpu"`. It returns the collection
time, namespace scope and `metric="scheduled_pod_requests"`:

- `allocated`: effective GPU requests of non-terminal Pods bound to a node.
- `running_allocated`: the subset whose Pod phase is Running.
- `unscheduled_requested`: requests of non-terminal Pods without a node binding.
- `by_namespace` and `gpu_pods`: supporting totals and workload summaries.

Succeeded/Failed Pods are excluded. GPU requests fall back to limits when absent.
Effective requests account for regular, init and restartable init containers and
Pod overhead, following [Kubernetes resource accounting](https://kubernetes.io/docs/concepts/workloads/pods/sidecar-containers/)
and [GPU request defaults](https://kubernetes.io/docs/tasks/manage-gpus/scheduling-gpus/).
The action fails rather than reporting zero when Pod specs or a complete list
cannot be obtained. These are scheduled resource units, not GPU compute utilization
or physical hardware capacity. MIG and other resource types must be queried by
their own resource_name; they are not added to physical GPU totals.

Regenerate/check the six contracts with `python scripts/generate_service_contracts.py`
and `python scripts/generate_service_contracts.py --check`.
