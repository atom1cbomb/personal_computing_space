"""Grade calculations and the CS101 origin/credential boundary, without live data."""
import json
from pathlib import Path
from statistics import pstdev
import time

import httpx
import pytest

from capability_services.cs101_http import server
from capability_services.cs101_http.server import CS101Gateway, GatewayError
from loom_v2.contracts.io_schema import validate


ROOT = Path(__file__).resolve().parents[2] / "capability_services/cs101_http"
PROFILE = {"uid": 99, "name": "bot", "role": 25, "group": ""}


def grade(status_id, user_id, point, *, paper_id=1, group="A", recorded_time=None):
    return {"status_id": status_id, "time": status_id * 1000 if recorded_time is None else recorded_time,
            "point": point, "context": '[{"points": 5, "context": "untrusted"}]',
            "instance": {"paper_instance_id": status_id + 100, "point": 0,
                         "paper": {"paper_id": paper_id, "title": f"Paper {paper_id}", "category": ["quiz"]},
                         "user": {"uid": user_id, "name": "Same name", "group": group,
                                  "email": "private@example.invalid", "role": 1}}}


GRADES = [grade(1, 10, 80), grade(2, 20, 100), grade(3, 30, 70, group="B"),
          grade(4, 40, 70, group="B"), grade(5, 10, 5, paper_id=2), grade(6, 10, 0)]


def provider(request, records=None):
    """Only the package's documented read endpoints are accepted by the fixture."""
    assert request.method == "GET"
    path, params = request.url.path, request.url.params
    if path == "/api/v2/users/me":
        return httpx.Response(200, json=PROFILE)
    if path == "/api/v2/papers/1/statistics":
        return httpx.Response(200, json={"users": [{"name": "Same name", "point": 0, "context": "opaque"}],
            "problems": [{"problem_id": 7, "statement": "https://outside.invalid/never-fetch",
                          "config": "opaque", "render_type": "choice"}]})
    if path == "/api/v2/papers":
        items = [{"paper_id": 1, "title": "Quiz", "category": ["quiz"],
                  "program": "never execute provider code"}]
    elif path == "/api/v2/paper-statuses":
        items = list(GRADES if records is None else records)
        for key in ("paper_id", "category", "group"):
            if key in params:
                if key == "paper_id":
                    items = [g for g in items if g["instance"]["paper"][key] == int(params[key])]
                elif key == "category":
                    items = [g for g in items if params[key] in g["instance"]["paper"][key]]
                else:
                    items = [g for g in items if g["instance"]["user"][key] == params[key]]
    else:
        pytest.fail(f"Unexpected provider path: {path}")
    offset, limit = int(params["offset"]), int(params["limit"])
    return httpx.Response(200, json={"items": items[offset:offset + limit], "count": len(items)})


def gateway(tmp_path, handler=provider, *, origin="https://test.cs101.gbu.edu.cn", token="private-fixture-token"):
    tmp_path.mkdir(parents=True, exist_ok=True)
    instance = tmp_path / "instance.json"
    instance.write_text(json.dumps({"api_origin": origin}))
    credential_root = tmp_path / "credentials"
    credential_root.mkdir(exist_ok=True)
    (credential_root / server.TOKEN_FILENAME).write_text(token)
    return CS101Gateway(instance_path=instance, credential_root=credential_root, state_root=tmp_path / "state",
                        transport=httpx.MockTransport(handler))


def call(service, action, payload):
    path = next(p for p, a in server.ROUTES.items() if a == action)
    result = service.handle(path, payload)
    schema = json.loads((ROOT / "schemas" / f"{action}-output.json").read_text())
    assert not validate(schema, result)
    return result["result"]


@pytest.mark.parametrize("origin", [
    "https://test.cs101.gbu.edu.cn", "https://another-school.example:8443/",
])
def test_configurable_origin_uses_only_fixed_get_paths_and_private_token(tmp_path, origin, monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://must-not-use.invalid")
    calls = []
    def upstream(request):
        calls.append(request)
        assert str(request.url).startswith(origin.rstrip("/") + "/api/v2/")
        assert request.headers["Authorization"] == "Bearer private-fixture-token"
        return provider(request)
    service = gateway(tmp_path, upstream, origin=origin)
    try:
        assert service.handle("/v1/auth/start", {}) == {"status": "authenticated"}
        assert service.handle("/v1/auth/status", {}) == {"status": "authenticated"}
        papers = call(service, "papers-list", {"keyword": "Quiz & exam", "limit": 1})
        assert papers["items"] == [{"paper_id": 1, "title": "Quiz", "category": ["quiz"]}]
        assert calls[-1].url.params["keyword"] == "Quiz & exam"
        assert all(r.method == "GET" for r in calls)
        assert "private-fixture-token" not in json.dumps(papers)
        assert "program" not in json.dumps(papers)
        assert service.client.trust_env is False
    finally:
        service.close()


@pytest.mark.parametrize("origin", [
    "https://iaaa.gbu.edu.cn", "https://IAAA.GBU.EDU.CN.:443/", "https://sso.iaaa.gbu.edu.cn",
    "http://test.cs101.gbu.edu.cn", "https://user:password@test.cs101.gbu.edu.cn",
    "https://test.cs101.gbu.edu.cn/api/v2", "https://test.cs101.gbu.edu.cn?token=secret",
    "https://test.cs101.gbu.edu.cn/#fragment", "https://test.cs101.gbu.edu.cn:0",
    "https://test.cs101.gbu.edu.cn:99999", "https://test.cs101.gbu.edu.cn\\@outside.invalid",
    "https://test.cs101.gbu.edu.cn\n", "https://test.cs101.gbu.edu.cn?",
])
def test_prohibited_or_invalid_origin_fails_before_network(tmp_path, origin):
    with pytest.raises(GatewayError, match="instance_config_invalid"):
        gateway(tmp_path, lambda _: pytest.fail("No network allowed"), origin=origin)


@pytest.mark.parametrize("location", ["https://iaaa.gbu.edu.cn/login", "https://outside.invalid/", "/api/v2/users/me"])
def test_redirects_are_never_followed_or_returned(tmp_path, location):
    calls = []
    def upstream(request):
        calls.append(request)
        return httpx.Response(302, headers={"Location": location}, text="private redirect detail")
    service = gateway(tmp_path, upstream)
    try:
        with pytest.raises(GatewayError, match="^upstream_redirect_blocked$"):
            service.handle("/v1/papers/list", {})
        assert len(calls) == 1 and calls[0].url.host == "test.cs101.gbu.edu.cn"
        assert service.auth_status() == {"status": "auth_failed"}
        assert len(calls) == 2
    finally:
        service.close()


@pytest.mark.parametrize("path,payload", [
    ("/v1/grades/list", {"api_origin": "https://iaaa.gbu.edu.cn"}),
    ("/v1/grades/list", {"url": "https://outside.invalid"}),
    ("/v1/papers/list", {"limit": 201}), ("/v1/grades/list", {"offset": -1}),
    ("/v1/grades/analyze", {"paper_id": True}), ("/v1/grades/analyze", {}),
    ("/v1/student/analyze", {"user_id": "../users"}),
    ("/v1/grades/analyze", {"paper_id": 1, "max_score": 0}),
    ("/v1/grades/analyze", {"paper_id": 1, "pass_score": float("nan")}),
    ("/v1/grades/analyze", {"paper_id": 1, "max_score": 10, "pass_score": 60}),
    ("/v1/oauth/login", {}), ("/v1/ncf/code/users/me", {}), ("/v1/grades/update", {}),
])
def test_invalid_actions_or_arguments_never_contact_provider(tmp_path, path, payload):
    service = gateway(tmp_path, lambda _: pytest.fail("No network allowed"))
    try:
        with pytest.raises(GatewayError):
            service.handle(path, payload)
        for remote in ("https://outside.invalid", "//iaaa.gbu.edu.cn", "/api/v2/oauth/login", "/api/v2/ncf/code/users/me"):
            with pytest.raises(GatewayError, match="upstream_path_not_allowed"):
                service._get(remote, {}, time.monotonic() + 10)
    finally:
        service.close()


def test_complete_analysis_deduplicates_users_counts_zero_and_competition_ties(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "PAGE_SIZE", 2)
    calls = []
    def upstream(request):
        calls.append(request)
        return provider(request)
    service = gateway(tmp_path, upstream)
    try:
        result = call(service, "grades-analyze", {"paper_id": 1, "pass_score": 60, "max_score": 100, "include_students": True})
        assert [int(r.url.params["offset"]) for r in calls] == [0, 2, 4]
        assert all(r.url.params["latest"] == "true" and r.url.params["all_user"] == "true" for r in calls)
        assert result["complete"] is True
        assert (result["source_record_count"], result["selected_record_count"]) == (5, 4)
        summary = result["summary"]
        assert (summary["count"], summary["min"], summary["max"], summary["mean"], summary["median"]) == (4, 0, 100, 60, 70)
        assert summary["stddev"] == pytest.approx(pstdev([0, 100, 70, 70]))
        assert summary["pass_count"] == 3 and summary["pass_rate_percent"] == 75
        assert summary["mean_percent"] == 60
        assert summary["distribution"] == [{"point": 0, "count": 1}, {"point": 70, "count": 2}, {"point": 100, "count": 1}]
        assert [(g["user_id"], g["rank"]) for g in result["students"]] == [(20, 1), (30, 2), (40, 2), (10, 4)]
        assert [g["summary"]["mean"] for g in result["by_group"]] == [50, 70]
        assert "private@example.invalid" not in json.dumps(result)
        assert "context" not in json.dumps(result)
        default = call(service, "grades-analyze", {"paper_id": 1})
        assert default["students"] == [] and default["students_included"] is False
        assert all(default["summary"][k] is None for k in ("pass_score", "max_score", "pass_rate_percent", "mean_percent"))
        grouped = call(service, "grades-analyze", {"paper_id": 1, "group": "B"})
        assert grouped["summary"]["count"] == 2 and grouped["summary"]["mean"] == 70
    finally:
        service.close()


def test_student_analysis_uses_ids_and_compares_within_each_paper(tmp_path):
    service = gateway(tmp_path)
    try:
        result = call(service, "student-analyze", {"user_id": 10, "category": "quiz"})
        assert result["paper_count"] == 2 and "mean" not in result
        assert [(p["grade"]["paper_id"], p["grade"]["point"], p["rank"], p["cohort"]["count"])
                for p in result["papers"]] == [(2, 5, 1, 1), (1, 0, 4, 4)]
        assert call(service, "student-analyze", {"user_id": 999})["papers"] == []
    finally:
        service.close()


def test_paged_grades_opt_in_context_and_problem_statistics_preserve_opaque_text(tmp_path):
    calls = []
    def upstream(request):
        calls.append(request)
        return provider(request)
    service = gateway(tmp_path, upstream)
    try:
        page = call(service, "grades-list", {"paper_id": 1, "limit": 2, "include_context": True})
        assert page["count"] == 5 and page["next_offset"] == 2
        assert page["items"][0]["context"] == GRADES[0]["context"]
        assert "include_context" not in calls[-1].url.params
        assert "context" not in call(service, "grades-list", {"limit": 1})["items"][0]
        stats = call(service, "paper-statistics", {"paper_id": 1})
        assert stats["users"][0]["point"] == 0
        assert stats["problems"][0]["statement"] == "https://outside.invalid/never-fetch"
        assert len(calls) == 3 and all(r.url.host == "test.cs101.gbu.edu.cn" for r in calls)
    finally:
        service.close()


@pytest.mark.parametrize("items,count", [(None, 0), ([], 0)])
def test_empty_data_is_not_a_zero_average_or_missing_student_roster(tmp_path, items, count):
    service = gateway(tmp_path, lambda _: httpx.Response(200, json={"items": items, "count": count}))
    try:
        result = call(service, "grades-analyze", {"paper_id": 1, "pass_score": 60})
        assert result["summary"]["count"] == 0
        assert all(result["summary"][k] is None for k in ("min", "max", "mean", "median", "stddev", "pass_rate_percent"))
        assert result["summary"]["pass_count"] == 0
    finally:
        service.close()


@pytest.mark.parametrize("failure", ["duplicate", "truncated", "changed", "limit"])
def test_incomplete_or_unstable_pages_never_produce_statistics(tmp_path, monkeypatch, failure):
    monkeypatch.setattr(server, "PAGE_SIZE", 2)
    def upstream(request):
        offset = int(request.url.params["offset"])
        items = [GRADES[0], GRADES[1]] if offset == 0 else ([] if failure == "truncated" else [GRADES[0]])
        count = 10001 if failure == "limit" else (4 if offset and failure == "changed" else 3)
        return httpx.Response(200, json={"items": items, "count": count})
    service = gateway(tmp_path, upstream)
    try:
        expected = {"duplicate": "upstream_list_incomplete", "truncated": "upstream_list_incomplete",
                    "changed": "upstream_list_changed", "limit": "analysis_record_limit_exceeded"}[failure]
        with pytest.raises(GatewayError, match=expected):
            service.handle("/v1/grades/analyze", {"paper_id": 1})
    finally:
        service.close()


@pytest.mark.parametrize("status,code,auth", [
    (401, "upstream_auth_required", "auth_required"), (403, "upstream_forbidden", "auth_failed"),
    (404, "upstream_not_found", "auth_failed"), (429, "upstream_rate_limited", "auth_failed"),
    (500, "upstream_request_failed", "auth_failed"),
])
def test_upstream_failures_are_sanitized_and_auth_distinguishes_permissions(tmp_path, status, code, auth):
    service = gateway(tmp_path, lambda _: httpx.Response(status, text="private-fixture-token and private provider detail"))
    try:
        with pytest.raises(GatewayError, match=f"^{code}$"):
            service.handle("/v1/papers/list", {})
        assert service.auth_status() == {"status": auth}
    finally:
        service.close()


@pytest.mark.parametrize("data", [[], {"items": []}, {"count": False, "items": []},
                                       {"count": 1, "items": [None]}, {"count": 0, "items": [GRADES[0]]}])
def test_malformed_upstream_data_fails_without_successful_empty_statistics(tmp_path, data):
    service = gateway(tmp_path, lambda _: httpx.Response(200, json=data))
    try:
        with pytest.raises(GatewayError, match="upstream_response_invalid"):
            service.handle("/v1/grades/analyze", {"paper_id": 1})
    finally:
        service.close()


def test_html_login_invalid_json_size_and_timeout_are_not_success(tmp_path, monkeypatch):
    responses = [httpx.Response(200, text="<html>login</html>"),
                 httpx.Response(200, content=b'{"items":', headers={"Content-Type": "application/json"}),
                 httpx.Response(200, json={"items": [], "count": 0, "extra": "x" * 100})]
    service = gateway(tmp_path, lambda _: responses.pop(0))
    try:
        for _ in range(2):
            with pytest.raises(GatewayError, match="upstream_response_invalid"):
                service.handle("/v1/grades/analyze", {"paper_id": 1})
        monkeypatch.setattr(server, "MAX_RESPONSE", 64)
        with pytest.raises(GatewayError, match="upstream_response_too_large"):
            service.handle("/v1/grades/analyze", {"paper_id": 1})
        with pytest.raises(GatewayError, match="analysis_timeout"):
            service._collect({}, time.monotonic() - 1)
    finally:
        service.close()
    def timeout(_request):
        raise httpx.ReadTimeout("private detail")
    service = gateway(tmp_path, timeout)
    try:
        with pytest.raises(GatewayError, match="^upstream_timeout$"):
            service.handle("/v1/grades/analyze", {"paper_id": 1})
        assert service.auth_status() == {"status": "auth_failed"}
    finally:
        service.close()


def test_missing_identity_and_ignored_scope_fail_instead_of_mixing_students(tmp_path):
    record = grade(1, 10, 50)
    del record["instance"]["user"]
    service = gateway(tmp_path, lambda _: httpx.Response(200, json={"items": [record], "count": 1}))
    try:
        with pytest.raises(GatewayError, match="upstream_grade_identity_missing"):
            service.handle("/v1/grades/analyze", {"paper_id": 1})
        record["instance"]["user"] = {"uid": 10, "name": "Student", "group": "A"}
        with pytest.raises(GatewayError, match="upstream_scope_mismatch"):
            service.handle("/v1/grades/analyze", {"paper_id": 1, "group": "B"})
    finally:
        service.close()


def test_credentials_reloaded_for_reauthentication_and_isolated_per_instance(tmp_path):
    calls = []
    def upstream(request):
        calls.append(request)
        return provider(request) if request.headers["Authorization"] == "Bearer renewed-token" else httpx.Response(401)
    first = gateway(tmp_path / "first", upstream)
    second = gateway(tmp_path / "second", upstream, token="renewed-token", origin="https://second-school.example")
    try:
        assert first.auth_status() == {"status": "auth_required"}
        assert second.auth_status() == {"status": "authenticated"}
        credential = first.credential_root / server.TOKEN_FILENAME
        credential.write_text("renewed-token")
        assert first.auth_status() == {"status": "authenticated"}
        credential.unlink()
        assert first.auth_status() == {"status": "auth_required"}
        credential.symlink_to(second.credential_root / server.TOKEN_FILENAME)
        assert first.auth_status() == {"status": "auth_required"}
        assert second.auth_status() == {"status": "authenticated"}
        assert [r.url.host for r in calls] == ["test.cs101.gbu.edu.cn", "second-school.example", "test.cs101.gbu.edu.cn", "second-school.example"]
    finally:
        first.close()
        second.close()


def browser_start(service, session_id="s" * 43):
    context = {"session_id": session_id, "callback_uri": "http://localhost:3000/login-callback",
               "expires_at": time.time() + 240}
    assert service.handle("/v1/auth/start", context)["status"] == "auth_required"
    return context


def browser_open(service, context):
    return service.handle("/v1/auth/browser", {"action": "open", "session_id": context["session_id"]})


def browser_callback(service, context, token):
    return service.handle("/v1/auth/browser", {"action": "callback", "session_id": context["session_id"],
                                               "query": {"token": [json.dumps(token)]}})


def test_browser_storage_login_verifies_only_cs101_and_survives_restart(tmp_path):
    requests = []
    def upstream(request):
        requests.append(request)
        assert request.method == "GET" and request.url.host == "test.cs101.gbu.edu.cn"
        assert request.headers["Authorization"] == "Bearer browser-private-token"
        return provider(request)
    service = gateway(tmp_path, upstream)
    (service.credential_root / server.TOKEN_FILENAME).unlink()
    try:
        assert service.auth_status() == {"status": "auth_required"}
        context = browser_start(service)
        assert browser_open(service, context) == {
            "status": "redirect", "location": "https://test.cs101.gbu.edu.cn/login?manual=show"}
        assert not requests  # The gateway never fetches the login link or IAAA.
        assert browser_callback(service, context, "browser-private-token") == {"status": "accepted"}
        assert [r.url.path for r in requests] == ["/api/v2/users/me"]
        assert service.state_path.stat().st_mode & 0o777 == 0o600
        assert service.auth_status() == {"status": "authenticated"}
        with pytest.raises(GatewayError, match="browser_session_not_found"):
            browser_callback(service, context, "browser-private-token")
        page = call(service, "grades-list", {"limit": 1})
        assert "browser-private-token" not in json.dumps(page)
        restarted = CS101Gateway(instance_path=tmp_path / "instance.json", credential_root=service.credential_root,
                                  state_root=tmp_path / "state", transport=httpx.MockTransport(upstream))
        try:
            assert restarted.auth_status() == {"status": "authenticated"}
        finally:
            restarted.close()
        # A token cached for one HTTPS origin must never be used at another.
        (tmp_path / "instance.json").write_text(json.dumps({"api_origin": "https://second-school.example"}))
        other = CS101Gateway(instance_path=tmp_path / "instance.json", credential_root=service.credential_root,
                              state_root=tmp_path / "state", transport=httpx.MockTransport(
                                  lambda _: pytest.fail("Different origin must require its own login")))
        try:
            assert other.auth_status() == {"status": "auth_required"}
        finally:
            other.close()
    finally:
        service.close()


def test_rejected_storage_token_keeps_same_challenge_and_requires_fresh_verification(tmp_path):
    accepted = {"token": "fresh-token"}
    def upstream(request):
        return provider(request) if request.headers["Authorization"] == "Bearer " + accepted["token"] else httpx.Response(401)
    service = gateway(tmp_path, upstream)
    (service.credential_root / server.TOKEN_FILENAME).unlink()
    try:
        context = browser_start(service)
        browser_open(service, context)
        assert browser_callback(service, context, "") == {"status": "pending"}
        assert browser_callback(service, context, None) == {"status": "pending"}
        assert service._browser_session["phase"] == "bound"
        assert browser_callback(service, context, "expired-token") == {"status": "pending"}
        assert service._browser_session["expires_at"] == context["expires_at"]
        assert not service.state_path.exists()
        assert service.auth_status() == {"status": "auth_required"}
        assert browser_callback(service, context, "fresh-token") == {"status": "accepted"}
        assert service.auth_status() == {"status": "authenticated"}
        new = browser_start(service, "n" * 43)
        assert not service.state_path.exists()
        with pytest.raises(GatewayError, match="upstream_auth_required"):
            service.handle("/v1/grades/list", {})
        with pytest.raises(GatewayError, match="browser_session_not_found"):
            browser_callback(service, context, "fresh-token")
        browser_open(service, new)
        service._browser_session["expires_at"] = time.time() - 1
        with pytest.raises(GatewayError, match="browser_session_expired"):
            browser_callback(service, new, "fresh-token")
        assert service.auth_status() == {"status": "auth_required"}
    finally:
        service.close()


@pytest.mark.parametrize("status", [302, 403, 429, 500])
def test_browser_callback_provider_failures_are_terminal_and_sanitized(tmp_path, status):
    service = gateway(tmp_path, lambda _: httpx.Response(status, headers={"Location": "https://iaaa.gbu.edu.cn"},
                                                        text="private-token"))
    (service.credential_root / server.TOKEN_FILENAME).unlink()
    try:
        context = browser_start(service)
        browser_open(service, context)
        with pytest.raises(GatewayError, match="^browser_login_verification_failed$"):
            browser_callback(service, context, "private-token")
        assert service._browser_session is None and not service.state_path.exists()
        assert service.auth_status() == {"status": "auth_required"}
    finally:
        service.close()


def test_browser_context_and_callback_validation_never_guess_credentials(tmp_path):
    service = gateway(tmp_path, lambda _: pytest.fail("Malformed callbacks must not access provider"))
    (service.credential_root / server.TOKEN_FILENAME).unlink()
    try:
        with pytest.raises(GatewayError, match="browser_context_invalid"):
            service.handle("/v1/auth/start", {"session_id": "bad"})
        context = browser_start(service)
        with pytest.raises(GatewayError, match="browser_session_consumed"):
            browser_callback(service, context, "private-token")
        browser_open(service, context)
        with pytest.raises(GatewayError, match="browser_session_consumed"):
            browser_open(service, context)
        for value in (123, {"token": "private"}, "bad\ntoken", "x" * 4097):
            with pytest.raises(GatewayError, match="browser_callback_invalid"):
                browser_callback(service, context, value)
    finally:
        service.close()


def test_mounted_profile_cannot_be_overridden_by_interactive_login(tmp_path):
    service = gateway(tmp_path)
    try:
        with pytest.raises(GatewayError, match="operator_credentials_configured"):
            browser_start(service)
        assert service.auth_status() == {"status": "authenticated"}
        assert service._browser_session is None
    finally:
        service.close()
