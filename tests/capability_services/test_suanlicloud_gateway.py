import json
from pathlib import Path

import pytest

from capability_services.suanlicloud_http.server import SuanliGateway, SuanliGatewayError


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


def test_unconfirmed_signing_protocol_does_not_send_credentials(monkeypatch, tmp_path: Path) -> None:
    gateway = _gateway(tmp_path)

    def fail_if_called(*args, **kwargs):
        pytest.fail("must not make an unsigned request")

    monkeypatch.setattr("capability_services.suanlicloud_http.server.httpx.stream", fail_if_called)
    with pytest.raises(SuanliGatewayError, match="provider_signature_spec_required"):
        gateway.call("regions_list")
    assert gateway.auth_status()["status"] == "auth_failed"


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
    # A test-only signer isolates routing tests from the missing provider spec.
    monkeypatch.setattr(gateway, "_auth_headers", lambda *args: {"Authorization": "test-only"})
    gateway.call("regions_list")
    assert captured["method"] == "POST"
    assert captured["url"].endswith("/v1/meta?Action=DescribeRegions")
    assert captured["headers"]["Authorization"] == "test-only"
    assert captured["follow_redirects"] is False
    assert json.loads(captured["content"]) == {"Action": "DescribeRegions"}


def test_auth_status_normalizes_provider_auth_failure(monkeypatch, tmp_path: Path) -> None:
    gateway = _gateway(tmp_path)

    def fail(_operation):
        raise SuanliGatewayError("provider_auth_failed")

    monkeypatch.setattr(gateway, "call", fail)
    assert gateway.auth_status() == {"status": "auth_failed"}
