import json
from pathlib import Path

from capability_services.suanli_vm_ssh_http.server import VmSshGateway


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

    class Proc:
        returncode = 0

        def communicate(self, timeout=None):
            return b"ok", b""

    def fake_popen(args, **_kwargs):
        captured.append(args)
        return Proc()

    import capability_services.suanli_vm_ssh_http.server as module

    monkeypatch.setattr(module.subprocess, "Popen", fake_popen)
    result = gateway.exec("printf safe", 5)
    assert result["exit_code"] == 0
    assert any("10.0.0.7" in item for item in captured[0])
    assert "10.0.0.8" not in captured[0]
