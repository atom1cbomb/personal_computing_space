import json
from pathlib import Path

import httpx
import pytest

from capability_services.volcano_http.server import GatewayError, VolcanoGateway


def gateway(tmp_path, handler, origin="https://volcano.example:7443"):
    config = tmp_path / "instance.json"
    config.write_text(json.dumps({"api_origin": origin}))
    return VolcanoGateway(instance_path=config, transport=httpx.MockTransport(handler))


def item(name, namespace="test", **kwargs):
    return {"metadata": {"name": name, "namespace": namespace}, **kwargs}


def pod(name, amount=0, *, namespace="test", phase="Running", node="node-a", **spec):
    return item(name, namespace, status={"phase": phase}, spec={
        "containers": [{"resources": {"limits": {"nvidia.com/gpu": str(amount)}}}],
        **({"nodeName": node} if node else {}), **spec,
    })


@pytest.mark.parametrize("port", [7443, 10062])
def test_uses_activation_origin_and_all_namespace_by_default(tmp_path, port):
    calls = []
    def provider(request):
        calls.append(request)
        return httpx.Response(200, json={"items": [item("training")], "totalCount": 1})
    with_gateway = gateway(tmp_path, provider, f"https://volcano.example:{port}")
    try:
        result = with_gateway.handle("/v1/podgroups/list", {})
        assert result["complete"] is True and result["total_count"] == 1
        assert calls[0].url.port == port
        assert calls[0].url.path == "/api/podgroups"
        assert calls[0].url.params["namespace"] == "All"
        with_gateway.handle("/v1/jobs/list", {"namespace": "dev-alice", "queue": "training", "search": "a & b"})
        assert calls[1].url.params["namespace"] == "dev-alice"
        assert calls[1].url.params["queue"] == "training"
        assert calls[1].url.params["search"] == "a & b"
    finally:
        with_gateway.close()


@pytest.mark.parametrize("origin", [
    "http://volcano.example", "https://user:password@volcano.example", "https://volcano.example/api",
    "https://volcano.example?token=secret", "https://volcano.example/#fragment",
    "https://volcano.example:99999", "https://volcano.example bad",
])
def test_invalid_activation_origin_is_rejected(tmp_path, origin):
    with pytest.raises(GatewayError, match="instance_config_invalid"):
        gateway(tmp_path, lambda _: httpx.Response(200), origin)


def test_queue_pagination_collects_all_pages(tmp_path):
    calls = []
    def provider(request):
        calls.append(request)
        page = int(request.url.params["page"])
        return httpx.Response(200, json={"items": [item("q-a"), item("q-b")] if page == 1 else [item("q-c")], "totalCount": 3})
    service = gateway(tmp_path, provider)
    result = service.handle("/v1/queues/list", {"state": "Open"})
    assert [x["metadata"]["name"] for x in result["items"]] == ["q-a", "q-b", "q-c"]
    assert result["total_count"] == 3
    assert [r.url.params["page"] for r in calls] == ["1", "2"]
    assert all(r.url.params["state"] == "Open" for r in calls)
    service.close()


def test_repeated_or_truncated_pages_do_not_produce_gpu_statistics(tmp_path):
    service = gateway(tmp_path, lambda _: httpx.Response(200, json={"items": [pod("p", 2)], "totalCount": 5}))
    with pytest.raises(GatewayError, match="dashboard_list_incomplete"):
        service.handle("/v1/gpu/usage", {})
    service.close()


@pytest.mark.parametrize("status,error", [(401, "dashboard_authentication_required"),
                                         (302, "dashboard_authentication_required"),
                                         (403, "dashboard_forbidden"), (500, "dashboard_request_failed")])
def test_upstream_errors_are_not_empty_successful_lists(tmp_path, status, error):
    service = gateway(tmp_path, lambda _: httpx.Response(status, text="private upstream detail"))
    with pytest.raises(GatewayError, match=f"^{error}$"):
        service.handle("/v1/pods/list", {})
    service.close()


@pytest.mark.parametrize("payload", [{"items": []}, {"items": [], "totalCount": "0"}, {"items": "not a list"}])
def test_invalid_list_counts_are_rejected(tmp_path, payload):
    service = gateway(tmp_path, lambda _: httpx.Response(200, json=payload))
    with pytest.raises(GatewayError, match="dashboard_response_invalid"):
        service.handle("/v1/pods/list", {})
    service.close()


def test_namespaces_endpoint_without_total_count(tmp_path):
    service = gateway(tmp_path, lambda _: httpx.Response(200, json={"items": [item("dev-alice")]}))
    assert service.handle("/v1/namespaces/list", {})["total_count"] == 1
    service.close()


def test_gpu_usage_distinguishes_allocation_running_and_pending_across_namespaces(tmp_path):
    pods = [pod("running", 2), pod("initializing", 1, phase="Pending"),
            pod("queued", 4, phase="Pending", node=None), pod("finished", 8, phase="Succeeded"),
            pod("other-user", 3, namespace="other"), pod("cpu-only")]
    service = gateway(tmp_path, lambda _: httpx.Response(200, json={"items": pods, "totalCount": len(pods)}))
    result = service.handle("/v1/gpu/usage", {})
    assert result["metric"] == "scheduled_pod_requests"
    assert (result["allocated"], result["running_allocated"], result["unscheduled_requested"]) == (6, 5, 4)
    assert result["terminal_pods"] == 1 and result["pod_count"] == 6
    assert result["by_namespace"] == [
        {"namespace": "other", "allocated": 3, "running_allocated": 3, "unscheduled_requested": 0, "gpu_pods": 1},
        {"namespace": "test", "allocated": 3, "running_allocated": 2, "unscheduled_requested": 4, "gpu_pods": 3},
    ]
    assert {p["name"] for p in result["gpu_pods"]} == {"running", "initializing", "queued", "other-user"}
    service.close()


def test_gpu_usage_respects_requested_namespace(tmp_path):
    service = gateway(tmp_path, lambda _: httpx.Response(200, json={"items": [pod("foreign", 1, namespace="other")], "totalCount": 1}))
    with pytest.raises(GatewayError, match="dashboard_namespace_mismatch"):
        service.handle("/v1/gpu/usage", {"namespace": "test"})
    service.close()


def test_gpu_request_uses_limits_default_and_init_sidecar_overhead_rules():
    def request(amount, **rest):
        return {"resources": {"requests": {"nvidia.com/gpu": amount}}, **rest}
    ordinary_init = pod("init", 2, initContainers=[request("8")])
    assert VolcanoGateway._pod_request(ordinary_init, "nvidia.com/gpu") == 8
    with_sidecars = pod("sidecars", 1,
        initContainers=[request("2", restartPolicy="Always"), request("5"), request("3", restartPolicy="Always")],
        overhead={"nvidia.com/gpu": "1"})
    assert VolcanoGateway._pod_request(with_sidecars, "nvidia.com/gpu") == 8
    assert VolcanoGateway._pod_request(pod("limits", 2), "nvidia.com/gpu") == 2
    explicit = pod("explicit", containers=[{"resources": {"requests": {"nvidia.com/gpu": "1"}, "limits": {"nvidia.com/gpu": "4"}}}])
    assert VolcanoGateway._pod_request(explicit, "nvidia.com/gpu") == 1
    mig = pod("mig", containers=[request("2")])
    assert VolcanoGateway._pod_request(mig, "nvidia.com/mig-1g.10gb") == 0


@pytest.mark.parametrize("value,expected", [("0", 0), ("2", 2), ("2000m", 2), ("1e2", 100)])
def test_integer_gpu_resource_quantities(value, expected):
    assert VolcanoGateway._quantity(value) == expected


@pytest.mark.parametrize("value", ["0.5", "500m", "-1", "NaN", "Infinity", True])
def test_invalid_gpu_quantities_are_not_silently_zero(value):
    with pytest.raises(GatewayError, match="gpu_quantity_invalid"):
        VolcanoGateway._quantity(value)


def test_actions_cannot_override_origin_or_call_dashboard_mutations(tmp_path):
    service = gateway(tmp_path, lambda _: pytest.fail("invalid arguments must not contact the provider"))
    for path, payload in [("/v1/queues/delete", {}), ("/v1/queues/list", {"url": "https://other.example"}),
                          ("/v1/pods/list", {"namespace": "--all-namespaces"})]:
        with pytest.raises(GatewayError):
            service.handle(path, payload)
    service.close()
