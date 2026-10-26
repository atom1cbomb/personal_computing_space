import json
from pathlib import Path
import time

import httpx
import pytest

from capability_services.autopaper_http import server
from capability_services.autopaper_http.server import AutoPaperGateway, GatewayError

PAPER = {"id": "paper-1", "title": "检索增强生成", "authors": ["Ada"], "abstract": "Abstract",
         "venue": "ACL", "year": 2025, "doi": None, "pdf_url": "https://papers.example/paper.pdf",
         "summary": {"problem": "Retrieval", "method": "Embeddings", "impact": "Better ranking"}}


def provider(request):
    assert request.url.host == "paper.cs.ac.cn"
    if request.headers.get("Cookie") != "ap_sid=private-session":
        return httpx.Response(401, text="private error detail")
    responses = {
        "/api/search": {"session_id": "search-1", "total": 1,
                        "papers": [{**PAPER, "similarity": .87}], "messages": [{"text": "Done"}]},
        "/api/papers/paper-1": PAPER,
        "/api/papers/list": [PAPER],
        "/api/papers/venues": [{"venue": "ACL", "count": 1}],
        "/api/papers/years": [{"year": 2025, "count": 1}],
        "/api/venues": {"total": 1, "venues": [{"abbrev": "ACL", "full_names": ["ACL Conference"],
                                               "ccf_category": "人工智能", "ccf_type": "C"}]},
    }
    return httpx.Response(200, json=responses[request.url.path])


def make_gateway(tmp_path, handler=provider, config=None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    instance = tmp_path / "instance.json"
    instance.write_text(json.dumps(config or {}))
    return AutoPaperGateway(instance_path=instance, state_root=tmp_path / "state",
                            transport=httpx.MockTransport(handler))


def begin(service, session_id="a" * 43):
    assert service.auth_start({"session_id": session_id, "callback_uri": "http://localhost:3000/login-callback",
                               "expires_at": time.time() + 300})["status"] == "auth_required"
    assert service.browser({"action": "open", "session_id": session_id}) == {
        "status": "redirect", "location": service.origin + "/oauth2/login"}
    return session_id


def login(service, session_id="a" * 43, cookie="private-session"):
    sid = begin(service, session_id)
    assert service.browser({"action": "callback", "session_id": sid,
                            "query": {"ap_sid": [cookie]}}) == {"status": "accepted"}


@pytest.fixture
def gateway(tmp_path):
    value = make_gateway(tmp_path)
    login(value)
    try:
        yield value
    finally:
        value.close()


@pytest.mark.parametrize("path,payload,method,upstream", [
    ("/v1/papers/search", {"keywords": ["RAG"], "description": "语义检索", "venues": ["ACL"],
                           "start_year": 2020, "end_year": 2025, "top_k": 10, "min_similarity": .7,
                           "session_id": "search-1"}, "POST", "/api/search"),
    ("/v1/papers/get", {"paper_id": "paper-1"}, "GET", "/api/papers/paper-1"),
    ("/v1/papers/list", {"venue": "ACL & EMNLP", "year": 2025}, "GET", "/api/papers/list"),
    ("/v1/venues/list", {}, "GET", "/api/papers/venues"),
    ("/v1/years/list", {"venue": "ACL & EMNLP"}, "GET", "/api/papers/years"),
    ("/v1/venues/catalog", {}, "GET", "/api/venues"),
])
def test_routes_preserve_provider_results_and_encode_filters(gateway, path, payload, method, upstream):
    calls = []
    def capture(request):
        calls.append(request)
        return provider(request)
    gateway.client.close()
    gateway.client = httpx.Client(transport=httpx.MockTransport(capture))
    value = gateway.handle(path, payload)
    request, = calls
    assert (request.method, request.url.path) == (method, upstream)
    assert value["action"] == server.ROUTES[path][0].replace("-", "_")
    assert value["result"] == provider(request).json()
    assert "private-session" not in json.dumps(value)
    if method == "POST":
        assert json.loads(request.content) == {**payload, "auto_ingest": False}
    else:
        assert dict(request.url.params) == {k: str(v) for k, v in payload.items() if k != "paper_id"}


def test_search_defaults_do_not_trigger_ingestion(gateway):
    def search(request):
        assert json.loads(request.content) == {"description": "graph learning", "top_k": 50,
                                               "min_similarity": .5, "auto_ingest": False}
        return provider(request)
    gateway.client.close()
    gateway.client = httpx.Client(transport=httpx.MockTransport(search))
    gateway.handle("/v1/papers/search", {"description": "graph learning"})


@pytest.mark.parametrize("path,payload", [
    ("/v1/papers/search", {}), ("/v1/papers/search", {"keywords": [" "]}),
    ("/v1/papers/search", {"description": "x", "auto_ingest": True}),
    ("/v1/papers/search", {"description": "x", "top_k": True}),
    ("/v1/papers/search", {"description": "x", "top_k": 201}),
    ("/v1/papers/search", {"description": "x", "min_similarity": float("nan")}),
    ("/v1/papers/search", {"description": "x", "start_year": 2025, "end_year": 2020}),
    ("/v1/papers/get", {"paper_id": "../admin"}),
    ("/v1/papers/get", {"paper_id": "x?token=secret"}),
    ("/v1/papers/list", {"venue": "ACL"}), ("/v1/years/list", {"venue": 2}),
    ("/v1/venues/list", {"api_origin": "https://other.example"}),
    ("/v1/venues/list", {"ap_sid": "secret"}),
    ("/v1/ingest", {}), ("/v1/auth/status", {"cookie": "secret"}),
])
def test_invalid_inputs_never_reach_upstream(tmp_path, path, payload):
    service = make_gateway(tmp_path, lambda _: pytest.fail("unexpected upstream request"))
    try:
        with pytest.raises(GatewayError):
            service.handle(path, payload)
    finally:
        service.close()


@pytest.mark.parametrize("origin", ["http://paper.cs.ac.cn", "https://user:secret@paper.cs.ac.cn",
    "https://paper.cs.ac.cn/api", "https://paper.cs.ac.cn?secret=x", "https://paper.cs.ac.cn/#x",
    "https://paper.cs.ac.cn:99999", "https://paper.cs.ac.cn bad"])
def test_invalid_origin_is_rejected(tmp_path, origin):
    with pytest.raises(GatewayError, match="instance_config_invalid"):
        make_gateway(tmp_path, config={"api_origin": origin})


def test_browser_login_persists_verified_cookie_and_restart_revalidates(tmp_path):
    service = make_gateway(tmp_path)
    try:
        assert service.auth_start({})["status"] == "auth_required"
        with pytest.raises(GatewayError, match="authentication_required"):
            service.handle("/v1/venues/list", {})
        login(service)
        assert service.auth_status() == {"status": "authenticated"}
        assert service.state_path.stat().st_mode & 0o777 == 0o600
    finally:
        service.close()
    restarted = make_gateway(tmp_path)
    try:
        assert restarted.auth_status() == {"status": "authenticated"}
        assert restarted.handle("/v1/papers/get", {"paper_id": "paper-1"})["result"] == PAPER
    finally:
        restarted.close()


def test_cookie_is_bound_to_origin_and_activation(tmp_path):
    first = make_gateway(tmp_path / "a")
    second = make_gateway(tmp_path / "b", lambda _: pytest.fail("no cookie should be sent"))
    try:
        login(first)
        assert second.auth_status()["status"] == "auth_required"
        with pytest.raises(GatewayError, match="browser_session_not_found"):
            second.browser({"action": "callback", "session_id": "a" * 43, "query": {"ap_sid": ["private-session"]}})
    finally:
        first.close()
        second.close()
    changed = make_gateway(tmp_path / "a", lambda _: pytest.fail("old origin cookie leaked"),
                           config={"api_origin": "https://other.example"})
    assert changed.auth_status()["status"] == "auth_required"
    changed.close()


def test_replaced_expired_and_replayed_browser_challenges(gateway, monkeypatch):
    old = begin(gateway)
    new = begin(gateway, "b" * 43)
    assert not gateway.state_path.exists()
    with pytest.raises(GatewayError, match="browser_session_not_found"):
        gateway.browser({"action": "callback", "session_id": old, "query": {"ap_sid": ["private-session"]}})
    with pytest.raises(GatewayError, match="browser_session_consumed"):
        gateway.browser({"action": "open", "session_id": new})
    now = time.time()
    monkeypatch.setattr(server.time, "time", lambda: now + 400)
    with pytest.raises(GatewayError, match="browser_session_expired"):
        gateway.browser({"action": "callback", "session_id": new, "query": {"ap_sid": ["private-session"]}})
    assert gateway.auth_status()["status"] == "auth_required"
    login(gateway, "c" * 43)
    with pytest.raises(GatewayError, match="browser_session_not_found"):
        gateway.browser({"action": "callback", "session_id": "c" * 43, "query": {"ap_sid": ["private-session"]}})


@pytest.mark.parametrize("cookie", ["", "a\nb", "x;other=secret", "x" * 4097])
def test_invalid_callback_cookie_is_rejected(tmp_path, cookie):
    service = make_gateway(tmp_path)
    try:
        sid = begin(service)
        with pytest.raises(GatewayError, match="browser_callback_invalid"):
            service.browser({"action": "callback", "session_id": sid, "query": {"ap_sid": [cookie]}})
        assert not service.state_path.exists()
    finally:
        service.close()


def test_failed_cookie_verification_never_becomes_ready(tmp_path):
    service = make_gateway(tmp_path)
    try:
        with pytest.raises(GatewayError, match="browser_login_verification_failed"):
            login(service, cookie="wrong-secret")
        assert not service.state_path.exists()
        assert service.auth_status()["status"] == "auth_required"
    finally:
        service.close()


@pytest.mark.parametrize("status,error,auth", [
    (401, "autopaper_authentication_required", "auth_required"),
    (302, "autopaper_authentication_required", "auth_required"),
    (403, "autopaper_forbidden", "auth_failed"),
    (404, "autopaper_not_found", "auth_failed"),
    (429, "autopaper_rate_limited", "auth_failed"),
    (500, "autopaper_request_failed", "auth_failed"),
])
def test_errors_are_sanitized_without_retry_or_redirect(gateway, status, error, auth):
    calls = []
    def failure(request):
        calls.append(request)
        return httpx.Response(status, text="private provider error",
                              headers={"Location": "https://untrusted.example"})
    gateway.client.close()
    gateway.client = httpx.Client(transport=httpx.MockTransport(failure))
    with pytest.raises(GatewayError, match=f"^{error}$"):
        gateway.handle("/v1/papers/search", {"description": "retrieval"})
    assert len(calls) == 1
    assert gateway.auth_status()["status"] == auth


@pytest.mark.parametrize("body", [b'<html>Login</html>', b'{}', b'[{}]', b'{"total": NaN}', b'{"total": 1e999}'])
def test_malformed_success_responses_fail_authentication(gateway, body):
    gateway.client.close()
    gateway.client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=body)))
    assert gateway.auth_status()["status"] == "auth_failed"
    with pytest.raises(GatewayError, match="autopaper_response_invalid"):
        gateway.handle("/v1/papers/search", {"keywords": ["retrieval"]})


def test_timeout_and_response_size_are_bounded(gateway, monkeypatch):
    def timeout(request):
        raise httpx.ReadTimeout("private endpoint details", request=request)
    gateway.client.close()
    gateway.client = httpx.Client(transport=httpx.MockTransport(timeout))
    with pytest.raises(GatewayError, match="^autopaper_timeout$") as error:
        gateway.handle("/v1/venues/list", {})
    assert error.value.status == 504
    assert gateway.auth_status()["status"] == "auth_failed"
    gateway.client.close()
    gateway.client = httpx.Client(transport=httpx.MockTransport(provider))
    monkeypatch.setattr(server, "MAX_RESPONSE", 10)
    with pytest.raises(GatewayError, match="autopaper_response_too_large"):
        gateway.handle("/v1/venues/list", {})


def test_implicit_cookie_jar_cannot_override_verified_session(gateway):
    gateway.client.cookies.set("ap_sid", "unverified-session", domain="paper.cs.ac.cn", path="/")
    gateway.client.cookies.set("other_secret", "untrusted", domain="paper.cs.ac.cn", path="/")
    assert gateway.auth_status() == {"status": "authenticated"}


async def test_http_health_and_input_errors(tmp_path):
    from tests.capability_services.test_grip_services import serve
    service = make_gateway(tmp_path)
    handler = type("TestHandler", (server.Handler,), {"gateway": service})
    try:
        async with serve(handler) as port:
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                assert (await client.get("/healthz")).json() == {"status": "ok"}
                assert (await client.post("/v1/auth/status", json={})).json()["status"] == "auth_required"
                for body in ('[]', '{', '{"description":"x","min_similarity":NaN}',
                             '{"description":"x","min_similarity":1e999}'):
                    assert (await client.post("/v1/papers/search", content=body)).status_code == 400
                assert (await client.post("/v1/papers/search", content=b"x" * (server.MAX_BODY + 1))).status_code == 413
                assert (await client.post("/v1/venues/list", json={})).status_code == 401
                assert (await client.post("/v1/auth/browser", json={"action": []})).status_code == 400
    finally:
        service.close()
