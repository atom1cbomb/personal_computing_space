import json
from pathlib import Path

from capability_services.vm_ssh_http.server import VmSshGateway
from loom_v2.contracts.io_schema import validate_schema
from loom_v2.contracts.types import IoContract
from loom_v2.digest import canonical_json_bytes, digest_bytes


def _gateway(tmp_path: Path) -> VmSshGateway:
    instance = tmp_path / "instance.json"
    instance.write_text(json.dumps({"host": "10.0.0.7", "port": 2222, "user": "ubuntu"}), encoding="utf-8")
    credentials = tmp_path / "credentials"
    credentials.mkdir()
    (credentials / "id_ed25519").write_text("private-key", encoding="utf-8")
    return VmSshGateway(instance_path=instance, credential_root=credentials, state_root=tmp_path / "state")


def test_gateway_uses_activation_target_and_credential_profile(tmp_path: Path):
    gateway = _gateway(tmp_path)
    args = gateway._ssh_args()
    assert args[-1] == "ubuntu@10.0.0.7"
    assert "-p" in args and "2222" in args
    assert "-i" in args and str(tmp_path / "credentials" / "id_ed25519") in args
    assert any(item.startswith("UserKnownHostsFile=") for item in args)


def test_gateway_never_accepts_request_target(tmp_path: Path, monkeypatch):
    gateway = _gateway(tmp_path)
    captured: list[list[str]] = []

    class Proc:
        returncode = 0

        def communicate(self, timeout=None):
            return b"ok", b""

    def fake_popen(args, **_kwargs):
        captured.append(args)
        return Proc()

    import capability_services.vm_ssh_http.server as module
    monkeypatch.setattr(module.subprocess, "Popen", fake_popen)

    result = gateway.handle("/v1/exec", {"command": "id", "host": "attacker.invalid", "user": "root"})
    assert result["exit_code"] == 0
    assert "attacker.invalid" not in captured[0]
    assert "ubuntu@10.0.0.7" in captured[0]
    assert captured[0][-2:] == ["ubuntu@10.0.0.7", "id"]


def test_gateway_reports_auth_required_without_private_key(tmp_path: Path):
    instance = tmp_path / "instance.json"
    instance.write_text(json.dumps({"host": "10.0.0.7", "user": "ubuntu"}), encoding="utf-8")
    gateway = VmSshGateway(instance_path=instance, credential_root=tmp_path / "missing", state_root=tmp_path / "state")
    assert gateway.auth_status() == {"status": "auth_required"}


def test_package_contract_refs_match_policy_validated_artifacts():
    root = Path(__file__).parents[2] / "capability_services" / "vm_ssh_http"
    manifest = json.loads((root / "package.example.json").read_text())
    contract = json.loads((root / "contracts/exec.io.json").read_text())
    parsed = IoContract.model_validate(contract)
    assert parsed.success_semantics is None
    for name, reference in (("exec-input.json", parsed.input_schema_ref), ("exec-output.json", parsed.output_schema_ref)):
        schema = json.loads((root / "schemas" / name).read_text())
        validate_schema(schema)
        assert reference.resource_id == f"content://sha256/{digest_bytes(canonical_json_bytes(schema))}"
    assert manifest["capability_exports"][0]["io_contract_ref"]["resource_id"] == f"content://sha256/{digest_bytes(canonical_json_bytes(contract))}"
    assert manifest["body"]["persistent_state"] is True
