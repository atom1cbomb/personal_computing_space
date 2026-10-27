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


def test_login_worker_is_configured_for_activation_local_durable_renewal(tmp_path, monkeypatch):
    calls = []
    def start(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(poll=lambda: None)
    monkeypatch.setattr(subprocess, "Popen", start)
    gateway = VmSshGateway(state_root=tmp_path)
    gateway._start_login()
    args, options = calls[0]
    assert args[:2] == ["loom-opkssh", "login"]
    assert args[args.index("--state-dir") + 1] == str(tmp_path)
    assert options["env"]["HOME"] == str(tmp_path)
    assert options["start_new_session"]


def test_restart_resumes_renewal_once_and_verifies_real_ssh_access(tmp_path, monkeypatch):
    (tmp_path / "renewal.json").write_text("{}")
    (tmp_path / "renewal-status.json").write_text('{"status":"authenticated"}')
    gateway = VmSshGateway(state_root=tmp_path)
    starts = []
    def start(mode):
        starts.append(mode)
        gateway._login_process = SimpleNamespace(poll=lambda: None)
    monkeypatch.setattr(gateway, "_start_login", start)
    monkeypatch.setattr(gateway, "_ssh_probe", lambda: "authenticated")
    for _ in range(3):
        assert gateway.auth_status() == {"status": "authenticated"}
    assert starts == ["resume"]
    monkeypatch.setattr(gateway, "_ssh_probe", lambda: "auth_failed")
    assert gateway.auth_status() == {"status": "auth_failed"}


def test_refresh_failure_recovers_but_revocation_requires_login(tmp_path, monkeypatch):
    (tmp_path / "renewal.json").write_text("{}")
    status = tmp_path / "renewal-status.json"
    gateway = VmSshGateway(state_root=tmp_path)
    gateway._login_process = SimpleNamespace(poll=lambda: None)
    monkeypatch.setattr(gateway, "_ssh_probe", lambda: "authenticated")
    status.write_text('{"status":"auth_failed"}')
    assert gateway.auth_status() == {"status": "auth_failed"}
    status.write_text('{"status":"authenticated"}')
    assert gateway.auth_status() == {"status": "authenticated"}
    status.write_text('{"status":"auth_required"}')
    gateway._login_process = None
    monkeypatch.setattr(gateway, "_start_login", lambda *_: pytest.fail("revoked token retried"))
    assert gateway.auth_status() == {"status": "auth_required"}


def test_expired_access_token_does_not_prompt_while_refresh_is_available(tmp_path, monkeypatch):
    (tmp_path / "renewal.json").write_text(json.dumps({"expires_at": time.time() - 1}))
    (tmp_path / "renewal-status.json").write_text('{"status":"authenticated"}')
    gateway = VmSshGateway(state_root=tmp_path)
    gateway._login_process = SimpleNamespace(poll=lambda: None)
    monkeypatch.setattr(gateway, "_ssh_probe", lambda: "auth_required")
    assert gateway.auth_status() == {"status": "auth_failed"}


def test_explicit_login_clears_stale_refresh_state_and_keys(tmp_path):
    gateway = VmSshGateway(state_root=tmp_path)
    gateway.ssh_dir.mkdir()
    files = [tmp_path / "renewal.json", tmp_path / "renewal-status.json",
             gateway.private_key, Path(str(gateway.private_key) + "-cert.pub")]
    for path in files:
        path.write_text("stale")
    try:
        gateway.auth_start({"session_id": "a" * 43, "callback_uri": "http://localhost:3000/login-callback",
                            "expires_at": time.time() + 200})
        assert all(not path.exists() for path in files)
    finally:
        gateway.close()


def test_successful_callback_keeps_renewal_worker_running(tmp_path, monkeypatch):
    import httpx
    gateway = VmSshGateway(state_root=tmp_path)
    context = {"session_id": "a" * 43, "callback_uri": "http://localhost:3000/login-callback",
               "expires_at": time.time() + 200}
    gateway.auth_start(context)
    gateway._browser_session["phase"] = "bound"
    process = SimpleNamespace(poll=lambda: None, wait=lambda **_: pytest.fail("waited for worker exit"))
    gateway._login_process = process
    def callback(*args, **kwargs):
        (tmp_path / "renewal.json").write_text("{}")
        (tmp_path / "renewal-status.json").write_text('{"status":"authenticated"}')
        return httpx.Response(200)
    monkeypatch.setattr(gateway, "_local_request", callback)
    try:
        assert gateway.browser({"action": "callback", "session_id": context["session_id"],
                                "query": {"code": ["code"], "state": ["state"]}}) == {"status": "accepted"}
        assert gateway._login_process is process
        assert gateway._browser_session is None
    finally:
        gateway._login_process = None
        gateway.close()
