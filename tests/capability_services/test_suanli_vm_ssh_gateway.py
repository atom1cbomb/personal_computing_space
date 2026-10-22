import json
import subprocess
import time
from types import SimpleNamespace
from pathlib import Path

from capability_services.suanli_vm_ssh_http.server import VmSshGateway
import pytest


@pytest.mark.parametrize("code,stderr,expected", [(0, b"", "authenticated"),
    (255, b"Permission denied (publickey).", "auth_required"),
    (255, b"Connection timed out", "auth_failed"), (255, b"REMOTE HOST IDENTIFICATION HAS CHANGED", "auth_failed")])
def test_ssh_authentication_expiry_is_distinct_from_connection_failure(tmp_path, monkeypatch, code, stderr, expected):
    gateway = VmSshGateway(state_root=tmp_path)
    monkeypatch.setattr(gateway, "_credentials_present", lambda: True)
    monkeypatch.setattr(gateway, "_ssh_args", lambda: ["ssh"])
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=code, stderr=stderr))
    assert gateway.auth_status() == {"status": expected}


def test_expired_ssh_login_can_restart_on_the_same_gateway(tmp_path):
    gateway = VmSshGateway(state_root=tmp_path)
    context = {"session_id": "a" * 43, "callback_uri": "http://localhost:3000/login-callback", "expires_at": time.time() + 200}
    try:
        gateway.auth_start(context)
        gateway._expire(context["session_id"])
        assert gateway.auth_status() == {"status": "auth_required"}
        gateway.auth_start({**context, "session_id": "b" * 43})
        assert gateway._browser_session["session_id"] == "b" * 43
        assert gateway.auth_status()["status"] == "auth_required"
    finally:
        gateway.close()


def test_vm_gateway_uses_activation_target_and_tofu_options(tmp_path: Path) -> None:
    instance = tmp_path / "instance.json"
    instance.write_text(
        json.dumps(
            {
                "target_resource_ref": {"resource_id": "grip://loom.local/workspaces/workspace-default/resources/suanli/vm/i-123"},
                "host": "10.0.0.7",
                "port": 22,
                "user": "root",
            }
        ),
        encoding="utf-8",
    )
    gateway = VmSshGateway(instance_path=instance, state_root=tmp_path / "state")
    args = gateway._ssh_args()
    assert "10.0.0.7" in args[-1]
    assert "-o" in args
    assert "StrictHostKeyChecking=accept-new" in args
    assert f"UserKnownHostsFile={tmp_path / 'state' / '.ssh' / 'known_hosts'}" in args
    assert "-o" in args and any(item.startswith("HostKeyAlias=loom-vm-") for item in args)


def test_vm_gateway_never_accepts_request_target(tmp_path: Path, monkeypatch) -> None:
    instance = tmp_path / "instance.json"
    instance.write_text(json.dumps({"host": "10.0.0.7", "port": 22, "user": "root"}), encoding="utf-8")
    gateway = VmSshGateway(instance_path=instance, state_root=tmp_path / "state")
    captured: list[list[str]] = []

    def fake_execute(args, command, timeout):
        captured.append([*args, command])
        return {"stdout": "ok", "stderr": "", "exit_code": 0, "timed_out": False, "truncated": False}

    import capability_services.suanli_vm_ssh_http.server as module

    monkeypatch.setattr(module, "execute_ssh", fake_execute)
    result = gateway.exec("printf safe", 5)
    assert result["exit_code"] == 0
    assert any("10.0.0.7" in item for item in captured[0])
    assert "10.0.0.8" not in captured[0]
