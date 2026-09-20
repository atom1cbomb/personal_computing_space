import json
from pathlib import Path

import pytest

from capability_services.outline_http.server import (
    DEFAULT_ENDPOINT,
    OutlineGateway,
    OutlineGatewayError,
)


def _gateway(tmp_path: Path) -> OutlineGateway:
    (tmp_path / "outline_key").write_text("ol_api_test-key", encoding="utf-8")
    return OutlineGateway(credential_root=tmp_path)


def test_default_endpoint_and_profile_are_used(tmp_path: Path) -> None:
    gateway = _gateway(tmp_path)
    assert gateway.endpoint == DEFAULT_ENDPOINT
    assert gateway._api_key() == "ol_api_test-key"


def test_profile_is_required_and_method_is_allowlisted(tmp_path: Path) -> None:
    gateway = _gateway(tmp_path)
    with pytest.raises(OutlineGatewayError, match="operation_not_allowed"):
        gateway.call("arbitrary")
    (tmp_path / "outline_key").unlink()
    with pytest.raises(OutlineGatewayError, match="credential_profile_unavailable"):
        gateway.call("documents_list")


def test_key_is_sent_as_bearer_and_secret_is_not_in_response(monkeypatch, tmp_path: Path) -> None:
    gateway = _gateway(tmp_path)
    captured: dict[str, object] = {}

    class Response:
        status_code = 200
        headers: dict[str, str] = {}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def iter_bytes(self):
            yield json.dumps({"ok": True, "data": []}).encode()

    def fake_stream(method, url, **kwargs):
        captured.update(method=method, url=url, **kwargs)
        return Response()

    monkeypatch.setattr("capability_services.outline_http.server.httpx.stream", fake_stream)
    result = gateway.call("documents_list", {"limit": 5})
    assert captured["method"] == "POST"
    assert captured["url"] == DEFAULT_ENDPOINT + "/documents.list"
    assert captured["headers"]["Authorization"] == "Bearer ol_api_test-key"
    assert json.loads(captured["content"]) == {"limit": 5}
    assert result == {"ok": True, "data": []}


def test_provider_auth_failure_is_normalized(monkeypatch, tmp_path: Path) -> None:
    gateway = _gateway(tmp_path)

    class Response:
        status_code = 401
        headers: dict[str, str] = {}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def iter_bytes(self):
            yield b'{"ok":false,"error":"unauthenticated"}'

    monkeypatch.setattr(
        "capability_services.outline_http.server.httpx.stream",
        lambda *_args, **_kwargs: Response(),
    )
    with pytest.raises(OutlineGatewayError, match="provider_auth_failed"):
        gateway.call("documents_list")
    assert gateway.auth_status() == {"status": "auth_failed"}


def test_auth_paths_do_not_expose_auth_info(monkeypatch, tmp_path: Path) -> None:
    gateway = _gateway(tmp_path)
    monkeypatch.setattr(gateway, "call", lambda operation, parameters=None: {"data": {"name": "private"}})
    assert gateway.handle("/v1/auth/start", {}) == {"status": "authenticated"}
    assert gateway.handle("/v1/auth/status", {}) == {"status": "authenticated"}


def test_arbitrary_path_and_non_object_parameters_are_rejected(tmp_path: Path) -> None:
    gateway = _gateway(tmp_path)
    with pytest.raises(OutlineGatewayError, match="operation_not_allowed"):
        gateway.handle("/v1/../../documents/list", {})
    with pytest.raises(OutlineGatewayError, match="request_json_invalid"):
        gateway.handle("/v1/documents/list", {"parameters": []})
