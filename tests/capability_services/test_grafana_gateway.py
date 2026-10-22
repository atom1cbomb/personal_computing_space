import json
from pathlib import Path
import time

import httpx
import pytest

from capability_services.grafana_http import server
from capability_services.grafana_http.server import GatewayError, GrafanaGateway


GPU = {"name": "DCGM_FI_DEV_GPU_UTIL", "node_label": "Hostname", "index_label": "gpu",
       "uuid_label": "UUID", "unit": "percent"}
DATASOURCES = [{"uid": "prom-main", "name": "Prometheus", "type": "prometheus", "isDefault": True,
                "url": "https://private.example", "secureJsonData": {"token": "provider-secret"}}]


def gateway(tmp_path, provider, *, config=None, credential=None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    instance = tmp_path / "instance.json"
    instance.write_text(json.dumps(config or {"api_origin": "https://grafana.example:7443",
                                            "gpu_metric": GPU}))
    credentials = tmp_path / "credentials"
    credentials.mkdir(exist_ok=True)
    if credential:
        (credentials / credential[0]).write_text(credential[1])
    return GrafanaGateway(instance_path=instance, credential_root=credentials,
                          state_root=tmp_path / "state", transport=httpx.MockTransport(provider))


def upstream(result=None, *, warnings=None, infos=None, result_type="vector"):
    return {"status": "success", "data": {"resultType": result_type, "result": result or []},
            "warnings": warnings or [], "infos": infos or []}


def sample(index="0", value="42", node="node-a", at=1792576800):
    return {"metric": {"Hostname": node, "gpu": index, "UUID": "GPU-" + index}, "value": [at, value]}


def provider_with_metrics(calls, value):
    def handle(request):
        calls.append(request)
        return httpx.Response(200, json=DATASOURCES if request.url.path == "/api/datasources" else value)
    return handle


def test_datasources_hide_private_configuration_and_use_activation_credentials(tmp_path):
    calls = []
    service = gateway(tmp_path, provider_with_metrics(calls, upstream()), credential=("grafana_token", "fixture-secret"))
    try:
        result = service.handle("/v1/datasources/list", {})
        assert result == {"status": "ok", "complete": True, "items": [
            {"uid": "prom-main", "name": "Prometheus", "type": "prometheus", "is_default": True}]}
        assert calls[0].headers["Authorization"] == "Bearer fixture-secret"
        assert calls[0].url.host == "grafana.example" and calls[0].url.port == 7443
        assert "secret" not in json.dumps(result) and "private.example" not in json.dumps(result)
        assert service.auth_status() == {"status": "authenticated"}
    finally:
        service.close()


@pytest.mark.parametrize("status,expected", [(401, "auth_required"), (302, "auth_required"),
                                             (403, "auth_failed"), (503, "auth_failed")])
def test_authentication_probe_reports_missing_or_invalid_access(tmp_path, status, expected):
    service = gateway(tmp_path, lambda _: httpx.Response(status, text="private body"))
    assert service.auth_status()["status"] == expected
    with pytest.raises(GatewayError) as failure:
        service.handle("/v1/datasources/list", {})
    assert "private body" not in str(failure.value)
    service.close()


def test_cookie_rotation_persists_per_activation_and_source_replacement_takes_effect(tmp_path):
    seen = []
    def provider(request):
        seen.append(request.headers.get("Cookie"))
        return httpx.Response(200, json=DATASOURCES,
                              headers={"Set-Cookie": "grafana_session=rotated-session; Path=/; HttpOnly; Secure"})
    service = gateway(tmp_path, provider, credential=("grafana_session", "original-session"))
    assert service.auth_status()["status"] == "authenticated"
    assert service.auth_status()["status"] == "authenticated"
    assert seen == ["grafana_session=original-session", "grafana_session=rotated-session"]
    assert service.state_path.stat().st_mode & 0o777 == 0o600
    service.close()
    restarted = gateway(tmp_path, provider)
    restarted.auth_status()
    assert seen[-1] == "grafana_session=rotated-session"
    (tmp_path / "credentials/grafana_session").write_text("replacement-session")
    restarted.auth_status()
    assert seen[-1] == "grafana_session=replacement-session"
    restarted.close()


@pytest.mark.parametrize("source", ["browser", "profile"])
def test_required_token_rotation_recovers_health_and_survives_restart(tmp_path, source):
    calls = []
    def provider(request):
        cookie = request.headers.get("Cookie")
        calls.append((request.method, request.url.path, cookie))
        if request.url.path == "/api/user/auth-tokens/rotate":
            assert request.method == "POST" and cookie == "grafana_session=old-session"
            return httpx.Response(200, json={"message": "Token rotated"},
                                  headers={"Set-Cookie": "grafana_session=new-session; Path=/; Secure; HttpOnly"})
        if cookie == "grafana_session=old-session":
            return httpx.Response(401, json={"messageId": "session.token.rotate"})
        assert cookie == "grafana_session=new-session"
        return httpx.Response(200, json=DATASOURCES)
    credential = ("grafana_session", "old-session") if source == "profile" else None
    service = gateway(tmp_path, provider, credential=credential)
    if source == "browser":
        service._session_source, service._session_cookie = "browser", "old-session"
        service._persist_session()
    assert service.auth_status() == {"status": "authenticated"}
    assert [(method, path) for method, path, _ in calls] == [
        ("GET", "/api/datasources"), ("POST", "/api/user/auth-tokens/rotate"),
        ("GET", "/api/datasources")]
    assert json.loads(service.state_path.read_text())["cookie"] == "new-session"
    assert service.state_path.stat().st_mode & 0o777 == 0o600
    service.close()
    restarted = gateway(tmp_path, provider)
    assert restarted.auth_status() == {"status": "authenticated"}
    assert calls[-1][2] == "grafana_session=new-session"
    assert sum(method == "POST" for method, _, _ in calls) == 1
    restarted.close()


def test_business_query_rotation_retries_the_same_query_and_time(tmp_path):
    calls = []
    points = [sample(value="7")]
    def provider(request):
        calls.append(request)
        if request.url.path == "/api/datasources":
            return httpx.Response(200, json=DATASOURCES)
        if request.method == "POST":
            assert request.url.path == "/api/user/auth-tokens/rotate"
            return httpx.Response(200, json={"message": "Token rotated"},
                                  headers={"Set-Cookie": "grafana_session=next-session; Path=/; Secure"})
        if request.headers["Cookie"] == "grafana_session=original-session":
            return httpx.Response(401, json={"messageId": "session.token.rotate"})
        return httpx.Response(200, json=upstream(points))
    service = gateway(tmp_path, provider, credential=("grafana_session", "original-session"))
    result = service.handle("/v1/metrics/query", {"query": 'up{job="GPU & monitoring"}',
        "datasource_uid": "prom-main", "time": 1792576800})
    assert result["result"] == points and result["complete"]
    reads = [r for r in calls if r.url.path.endswith("/query")]
    assert len(reads) == 2 and reads[0].url == reads[1].url
    assert reads[1].url.params["query"] == 'up{job="GPU & monitoring"}'
    assert float(reads[1].url.params["time"]) == 1792576800
    assert reads[1].headers["Cookie"] == "grafana_session=next-session"
    service.close()


@pytest.mark.parametrize("failure,expected", [("expired", "auth_required"),
    ("unavailable", "auth_failed"), ("network", "auth_failed"),
    ("missing_cookie", "auth_failed"), ("repeated_challenge", "auth_failed"),
    ("retry_challenge", "auth_failed")])
def test_rotation_failure_is_bounded_and_not_mistaken_for_success(tmp_path, failure, expected):
    posts = []
    def provider(request):
        if request.method == "GET":
            return httpx.Response(401, json={"messageId": "session.token.rotate"})
        posts.append(request)
        if failure == "network":
            raise httpx.ConnectError("private upstream detail", request=request)
        if failure == "expired":
            return httpx.Response(401, text="private expired credential")
        if failure == "unavailable":
            return httpx.Response(503, text="private upstream detail")
        if failure == "repeated_challenge":
            return httpx.Response(401, json={"messageId": "session.token.rotate"})
        headers = {"Set-Cookie": "grafana_session=next-session; Path=/; Secure"} if failure == "retry_challenge" else {}
        return httpx.Response(200, json={"message": "Token rotated"}, headers=headers)
    service = gateway(tmp_path, provider, credential=("grafana_session", "old-session"))
    assert service.auth_status()["status"] == expected
    assert len(posts) == 1
    service.close()


@pytest.mark.parametrize("credential,status,body", [
    (("grafana_session", "expired-session"), 401, {"messageId": "session.token.expired"}),
    (("grafana_session", "revoked-session"), 401, {"error": {"id": "ERR_TOKEN_REVOKED"}}),
    (("grafana_session", "expired-session"), 302, None),
    (("grafana_token", "service-token"), 401, {"messageId": "session.token.rotate"}),
    (None, 401, {"messageId": "session.token.rotate"}),
])
def test_rotation_is_only_attempted_for_an_explicit_cookie_rotation_challenge(tmp_path, credential, status, body):
    calls = []
    def provider(request):
        calls.append(request)
        return httpx.Response(status, json=body)
    service = gateway(tmp_path, provider, credential=credential)
    assert service.auth_status()["status"] == "auth_required"
    assert len(calls) == 1 and calls[0].method == "GET"
    service.close()


def test_concurrent_health_checks_rotate_the_instance_session_once(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    rotations = []
    def provider(request):
        if request.method == "POST":
            rotations.append(request)
            return httpx.Response(200, json={"message": "Token rotated"},
                                  headers={"Set-Cookie": "grafana_session=new-session; Path=/; Secure"})
        if request.headers["Cookie"] == "grafana_session=old-session":
            return httpx.Response(401, json={"messageId": "session.token.rotate"})
        return httpx.Response(200, json=DATASOURCES)
    service = gateway(tmp_path, provider, credential=("grafana_session", "old-session"))
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: service.auth_status(), range(4)))
    assert results == [{"status": "authenticated"}] * 4
    assert len(rotations) == 1
    service.close()


def test_instances_use_independent_origins_profiles_and_session_state(tmp_path):
    seen = []
    def provider(request):
        seen.append((str(request.url), request.headers.get("Authorization")))
        return httpx.Response(200, json=DATASOURCES)
    for name in ("bj", "nj", "ict-bj"):
        service = gateway(tmp_path / name, provider, config={"api_origin": f"https://{name}.example:10062"},
                          credential=("grafana_token", "fixture-" + name))
        assert service.auth_status()["status"] == "authenticated"
        service.close()
    assert seen == [(f"https://{n}.example:10062/api/datasources", "Bearer fixture-" + n)
                    for n in ("bj", "nj", "ict-bj")]


def test_removing_source_cookie_does_not_keep_an_implicit_authenticated_cookie_jar(tmp_path):
    def provider(request):
        if "grafana_session=" not in request.headers.get("Cookie", ""):
            return httpx.Response(401)
        return httpx.Response(200, json=DATASOURCES, headers={"Set-Cookie": "grafana_session=rotated; Path=/"})
    service = gateway(tmp_path, provider, credential=("grafana_session", "original"))
    assert service.auth_status()["status"] == "authenticated"
    (tmp_path / "credentials/grafana_session").unlink()
    assert service.auth_status()["status"] == "auth_required"
    service.close()


@pytest.mark.parametrize("value", ["bad\nheader", "", "two cookies; grafana_session=x"])
def test_invalid_session_credentials_never_reach_upstream(tmp_path, value):
    service = gateway(tmp_path, lambda _: pytest.fail("credential invalid"), credential=("grafana_session", value))
    assert service.auth_status() == {"status": "auth_failed"}
    service.close()


def test_dashboard_pagination_and_detail(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "PAGE_SIZE", 2)
    calls = []
    def provider(request):
        calls.append(request)
        if request.url.path == "/api/search":
            page = int(request.url.params["page"])
            return httpx.Response(200, json=[{"uid": n, "title": n} for n in
                                            (("gpu", "nodes") if page == 1 else ("jobs",))])
        return httpx.Response(200, json={"dashboard": {"uid": "gpu", "title": "GPU", "panels": [{"targets": [{"expr": GPU["name"]}]}]}})
    service = gateway(tmp_path, provider)
    result = service.handle("/v1/dashboards/list", {"search": "GPU & nodes"})
    assert result["total_count"] == 3 and result["complete"]
    assert [r.url.params["page"] for r in calls] == ["1", "2"]
    assert calls[0].url.params["query"] == "GPU & nodes"
    dashboard = service.handle("/v1/dashboards/get", {"uid": "gpu"})
    assert dashboard["dashboard"]["panels"][0]["targets"][0]["expr"] == GPU["name"]
    service.close()


def test_repeated_dashboard_pages_fail_instead_of_silently_truncating(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "PAGE_SIZE", 1)
    service = gateway(tmp_path, lambda _: httpx.Response(200, json=[{"uid": "gpu", "title": "GPU"}]))
    with pytest.raises(GatewayError, match="grafana_dashboard_list_truncated"):
        service.handle("/v1/dashboards/list", {})
    service.close()


def test_prometheus_query_preserves_series_timestamps_and_partial_result_warnings(tmp_path):
    calls = []
    service = gateway(tmp_path, provider_with_metrics(calls, upstream([sample()], warnings=["partial data"], infos=["hint"])))
    result = service.handle("/v1/metrics/query", {"query": "up{job=\"gpu\"}", "time": "2026-10-21T10:00:00Z"})
    assert result["result"] == [sample()] and result["result_type"] == "vector"
    assert result["warnings"] == ["partial data"] and result["infos"] == ["hint"]
    assert result["complete"] is False
    assert calls[-1].url.path == "/api/datasources/proxy/uid/prom-main/api/v1/query"
    assert calls[-1].url.params["query"] == 'up{job="gpu"}'
    assert float(calls[-1].url.params["time"]) == 1792576800
    service.close()


def test_range_query_uses_explicit_interval_and_retains_native_matrix(tmp_path):
    calls = []
    matrix = [{"metric": {"gpu": "0"}, "values": [[10, "5"], [20, "8"]]}]
    service = gateway(tmp_path, provider_with_metrics(calls, upstream(matrix, result_type="matrix")))
    result = service.handle("/v1/metrics/query-range", {"query": "up", "start": 10, "end": 20, "step": 10})
    assert result["result"] == matrix and result["result_type"] == "matrix"
    assert calls[-1].url.path.endswith("/query_range")
    assert dict(calls[-1].url.params) == {"query": "up", "start": "10.0", "end": "20.0", "step": "10"}
    service.close()


@pytest.mark.parametrize("change", [{"step": 0}, {"step": True}, {"end": 9}, {"end": 20000},
                                   {"start": "NaN"}, {"start": "2026-10-21T10:00:00"}])
def test_invalid_or_excessive_time_ranges_are_not_submitted(tmp_path, change):
    service = gateway(tmp_path, lambda _: pytest.fail("invalid range"))
    with pytest.raises(GatewayError):
        service.handle("/v1/metrics/query-range", {"query": "up", "start": 10, "end": 20, "step": 1, **change})
    service.close()


@pytest.mark.parametrize("data,error", [
    ({"status": "error", "error": "upstream private details"}, "prometheus_query_failed"),
    ({"status": "success", "data": {}}, "prometheus_response_invalid"),
    ([], "prometheus_response_invalid"),
])
def test_query_errors_are_not_empty_successful_data(tmp_path, data, error):
    service = gateway(tmp_path, provider_with_metrics([], data))
    with pytest.raises(GatewayError, match="^" + error + "$"):
        service.handle("/v1/metrics/query", {"query": "up"})
    service.close()


@pytest.mark.parametrize("sources,config,expected", [
    ([{"uid": "a", "name": "a", "type": "prometheus"}, {"uid": "b", "name": "b", "type": "prometheus"}], {}, "datasource_ambiguous"),
    ([{"uid": "a", "name": "a", "type": "loki"}], {"datasource_uid": "a"}, "prometheus_datasource_required"),
    (DATASOURCES, {"datasource_uid": "missing"}, "datasource_not_found"),
])
def test_datasource_selection_does_not_guess(tmp_path, sources, config, expected):
    service = gateway(tmp_path, lambda _: httpx.Response(200, json=sources),
                      config={"api_origin": "https://grafana.example", **config})
    with pytest.raises(GatewayError, match=expected):
        service.handle("/v1/metrics/query", {"query": "up"})
    service.close()


def test_gpu_utilization_reports_numeric_identity_uuid_and_each_actual_sample(tmp_path):
    calls = []
    service = gateway(tmp_path, provider_with_metrics(calls, upstream([sample("2", "0"), sample("0", "83")])))
    result = service.handle("/v1/gpu/utilization", {"hostname": "node-a"})
    assert result["status"] == "ok" and result["unit"] == "percent" and result["complete"]
    assert [(s["gpu_index"], s["gpu_uuid"], s["utilization_percent"]) for s in result["samples"]] == [(0, "GPU-0", 83.0), (2, "GPU-2", 0.0)]
    assert result["samples"][0]["sampled_at"] == "2026-10-21T10:00:00+00:00"
    assert calls[-1].url.params["query"] == 'DCGM_FI_DEV_GPU_UTIL{Hostname="node-a"}'
    service.close()


def test_gpu_index_filter_and_ratio_conversion_are_explicit(tmp_path):
    calls = []
    service = gateway(tmp_path, provider_with_metrics(calls, upstream([sample("3", "0.75")])),
                      config={"api_origin": "https://grafana.example", "gpu_metric": {**GPU, "unit": "ratio"}})
    result = service.handle("/v1/gpu/utilization", {"hostname": "node-a", "gpu_index": 3, "time": 1792576800})
    assert result["samples"][0]["utilization_percent"] == 75
    assert calls[-1].url.params["query"] == 'DCGM_FI_DEV_GPU_UTIL{Hostname="node-a",gpu="3"}'
    service.close()


def test_missing_gpu_samples_are_no_data_instead_of_zero_utilization(tmp_path):
    service = gateway(tmp_path, provider_with_metrics([], upstream()))
    result = service.handle("/v1/gpu/utilization", {"hostname": "node-a"})
    assert result["status"] == "no_data" and result["samples"] == []
    service.close()


@pytest.mark.parametrize("point", [sample(value="NaN"), sample(value="101"), sample(value="-1"),
                                  sample(index="GPU-uuid"), sample(node="other-node"),
                                  {"metric": {"Hostname": "node-a"}, "value": [0, "2"]}])
def test_invalid_or_unidentified_gpu_samples_never_become_utilization(tmp_path, point):
    service = gateway(tmp_path, provider_with_metrics([], upstream([point])))
    with pytest.raises(GatewayError, match="gpu_metric_sample_invalid"):
        service.handle("/v1/gpu/utilization", {"hostname": "node-a"})
    service.close()


def test_gpu_selector_is_encoded_as_a_promql_string(tmp_path):
    calls = []
    service = gateway(tmp_path, provider_with_metrics(calls, upstream()))
    hostname = 'node"},evil{gpu="0'
    service.handle("/v1/gpu/utilization", {"hostname": hostname})
    assert calls[-1].url.params["query"] == GPU["name"] + '{Hostname=' + json.dumps(hostname) + '}'
    service.close()


@pytest.mark.parametrize("origin", ["http://grafana.example", "https://u:p@grafana.example", "https://grafana.example/api",
                                   "https://grafana.example?token=x", "https://grafana.example:99999"])
def test_invalid_activation_origin_is_rejected(tmp_path, origin):
    with pytest.raises(GatewayError, match="instance_config_invalid"):
        gateway(tmp_path, lambda _: httpx.Response(200), config={"api_origin": origin})


def test_actions_cannot_override_runtime_origin_or_mutate_dashboards(tmp_path):
    service = gateway(tmp_path, lambda _: pytest.fail("unsupported action"))
    for path, payload in (("/v1/dashboards/delete", {"uid": "gpu"}),
                          ("/v1/datasources/list", {"url": "https://other.example"}),
                          ("/v1/dashboards/get", {"uid": "../../admin"})):
        with pytest.raises(GatewayError):
            service.handle(path, payload)
    service.close()


def begin_browser(service, session_id="b" * 43):
    result = service.handle("/v1/auth/start", {"session_id": session_id,
        "callback_uri": "http://localhost:3000/login-callback", "expires_at": time.time() + 240})
    assert result["status"] == "auth_required"
    return session_id


def browser_provider(request):
    if request.headers.get("Cookie") != "grafana_session=browser-private-cookie":
        return httpx.Response(401)
    return httpx.Response(200, json=DATASOURCES,
                          headers={"Set-Cookie": "grafana_session=browser-rotated-cookie; Path=/; Secure; HttpOnly"})


def test_browser_auth_uses_public_login_origin_and_persists_only_a_verified_session(tmp_path):
    def provider(request):
        if request.headers.get("Cookie") == "grafana_session=browser-rotated-cookie":
            return httpx.Response(200, json=DATASOURCES)
        return browser_provider(request)
    config = {"api_origin": "https://grafana-internal.example", "browser_origin": "https://grafana-public.example:7443"}
    service = gateway(tmp_path, provider, config=config)
    assert service.auth_status()["status"] == "auth_required"
    session_id = begin_browser(service)
    opened = service.handle("/v1/auth/browser", {"action": "open", "session_id": session_id})
    assert opened == {"status": "redirect", "location": "https://grafana-public.example:7443/login/generic_oauth"}
    assert service.auth_status()["status"] == "auth_required"
    accepted = service.handle("/v1/auth/browser", {"action": "callback", "session_id": session_id,
        "query": {"grafana_session": ["browser-private-cookie"]}})
    assert accepted == {"status": "accepted"}
    assert "cookie" not in json.dumps(accepted)
    assert service.auth_status() == {"status": "authenticated"}
    assert json.loads(service.state_path.read_text())["cookie"] == "browser-rotated-cookie"
    assert service.state_path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(GatewayError, match="browser_session_not_found"):
        service.handle("/v1/auth/browser", {"action": "callback", "session_id": session_id,
            "query": {"grafana_session": ["browser-private-cookie"]}})
    service.close()
    restarted = gateway(tmp_path, provider, config=config)
    assert restarted.auth_status() == {"status": "authenticated"}
    restarted.close()


def test_invalid_browser_cookie_is_not_persisted_or_reported_as_authenticated(tmp_path):
    service = gateway(tmp_path, browser_provider)
    sid = begin_browser(service)
    service.browser({"action": "open", "session_id": sid})
    with pytest.raises(GatewayError, match="browser_login_verification_failed"):
        service.browser({"action": "callback", "session_id": sid, "query": {"grafana_session": ["wrong-private-cookie"]}})
    assert not service.state_path.exists()
    assert service.auth_status()["status"] == "auth_required"
    service.close()


def test_browser_callback_can_rotate_before_persisting_verified_credentials(tmp_path):
    calls = []
    def provider(request):
        calls.append(request)
        assert not service.state_path.exists()
        if request.method == "POST":
            return httpx.Response(200, json={"message": "Token rotated"},
                                  headers={"Set-Cookie": "grafana_session=verified-session; Path=/; Secure"})
        if request.headers.get("Cookie") == "grafana_session=old-browser-session":
            return httpx.Response(401, json={"messageId": "session.token.rotate"})
        assert request.headers["Cookie"] == "grafana_session=verified-session"
        return httpx.Response(200, json=DATASOURCES)
    service = gateway(tmp_path, provider)
    sid = begin_browser(service)
    service.browser({"action": "open", "session_id": sid})
    assert service.browser({"action": "callback", "session_id": sid,
                            "query": {"grafana_session": ["old-browser-session"]}}) == {"status": "accepted"}
    assert [r.method for r in calls] == ["GET", "POST", "GET"]
    assert json.loads(service.state_path.read_text())["cookie"] == "verified-session"
    service.close()


def test_browser_cookie_cannot_be_delivered_to_another_activation_session(tmp_path):
    a = gateway(tmp_path / "a", browser_provider)
    b = gateway(tmp_path / "b", browser_provider)
    sid = begin_browser(a, "a" * 43)
    begin_browser(b, "b" * 43)
    a.browser({"action": "open", "session_id": sid})
    with pytest.raises(GatewayError, match="browser_session_not_found"):
        b.browser({"action": "callback", "session_id": sid, "query": {"grafana_session": ["browser-private-cookie"]}})
    assert not a.state_path.exists() and not b.state_path.exists()
    a.close(); b.close()


def test_expired_browser_session_and_repeated_open_are_rejected(tmp_path, monkeypatch):
    service = gateway(tmp_path, browser_provider)
    sid = begin_browser(service)
    service.browser({"action": "open", "session_id": sid})
    with pytest.raises(GatewayError, match="browser_session_consumed"):
        service.browser({"action": "open", "session_id": sid})
    future = time.time() + 600
    monkeypatch.setattr(server.time, "time", lambda: future)
    with pytest.raises(GatewayError, match="browser_session_expired"):
        service.browser({"action": "callback", "session_id": sid, "query": {"grafana_session": ["browser-private-cookie"]}})
    assert not service.state_path.exists()
    service.close()


def test_gpu_mapping_can_be_supplied_after_browser_login_without_recreating_activation(tmp_path):
    calls = []
    service = gateway(tmp_path, provider_with_metrics(calls, upstream([sample("2", "37")])),
                      config={"api_origin": "https://grafana.example"})
    result = service.handle("/v1/gpu/utilization", {"hostname": "node-a", "gpu_index": 2,
        "datasource_uid": "prom-main", "gpu_metric": GPU})
    assert result["samples"][0]["utilization_percent"] == 37
    assert result["samples"][0]["gpu_index"] == 2
    service.close()
