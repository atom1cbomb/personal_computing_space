import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from capability_services.suanlicloud_http.server import (
    SuanliGateway,
    SuanliGatewayError,
    _build_auth_headers,
)


def _gateway(tmp_path: Path) -> SuanliGateway:
    (tmp_path / "AccessKeyId").write_text("AK-test", encoding="utf-8")
    (tmp_path / "SecretKey").write_text("secret-test", encoding="utf-8")
    return SuanliGateway(credential_root=tmp_path)


def test_profile_is_required_and_operation_is_allowlisted(tmp_path: Path) -> None:
    gateway = _gateway(tmp_path)
    with pytest.raises(SuanliGatewayError, match="operation_not_allowed"):
        gateway.call("arbitrary")
    (tmp_path / "SecretKey").unlink()
    with pytest.raises(SuanliGatewayError, match="credential_profile_unavailable"):
        gateway.call("regions_list")


def test_signer_uses_provider_protocol_without_exposing_secret() -> None:
    headers = _build_auth_headers(
        "AKtest1234567890abcd",
        "secret-1234567890abcdefghij1234567890abcdefghij",
        "https://cloud.example.com",
        "POST",
        "/v1",
        b'{"Action":"GetUserinfo"}',
        "global",
        "iam",
        now=lambda: datetime(2026, 5, 31, 8, 42, tzinfo=timezone.utc),
    )
    assert headers["Host"] == "cloud.example.com"
    assert headers["X-Suanli-Date"] == "2026-05-31T08:42:00Z"
    assert headers["Authorization"].startswith(
        "SUANLI-HMAC-SHA256 Credential=AKtest1234567890abcd/20260531/global/iam/suanli_request, "
    )
    assert headers["Authorization"].endswith(
        "Signature=16181970e3f40395df018006699eebf2b00802a094969c69c2568c18abcc1190"
    )
    assert "secret-1234567890" not in headers["Authorization"]


def test_missing_credentials_still_fails_closed(tmp_path: Path) -> None:
    gateway = _gateway(tmp_path)
    (tmp_path / "SecretKey").unlink()
    with pytest.raises(SuanliGatewayError, match="credential_profile_unavailable"):
        gateway.call("regions_list")


def test_operation_routes_to_documented_provider_action(monkeypatch, tmp_path: Path) -> None:
    gateway = _gateway(tmp_path)
    captured: dict[str, object] = {}

    class Response:
        status_code = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def iter_bytes(self):
            yield json.dumps({"Regions": []}).encode()

    def fake_stream(method, url, **kwargs):
        captured.update(method=method, url=url, **kwargs)
        return Response()

    monkeypatch.setattr("capability_services.suanlicloud_http.server.httpx.stream", fake_stream)
    # A test-only signer isolates routing from the external provider.
    monkeypatch.setattr(gateway, "_auth_headers", lambda *args: {"Authorization": "test-only"})
    gateway.call("regions_list")
    assert captured["method"] == "POST"
    assert captured["url"].endswith("/v1/meta")
    assert captured["headers"]["Authorization"] == "test-only"
    assert captured["follow_redirects"] is False
    assert json.loads(captured["content"]) == {"Action": "DescribeRegions"}


def test_auth_status_normalizes_provider_auth_failure(monkeypatch, tmp_path: Path) -> None:
    gateway = _gateway(tmp_path)

    def fail(_operation):
        raise SuanliGatewayError("provider_auth_failed")

    monkeypatch.setattr(gateway, "call", fail)
    assert gateway.auth_status() == {"status": "auth_failed"}
