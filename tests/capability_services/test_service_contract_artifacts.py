from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest

from loom_orchestration.naming import content_uri
from loom_v2.contracts.io_schema import validate, validate_schema
from loom_v2.contracts.resource_effects import validate_action_descriptor
from loom_v2.contracts.types import IoContract
from loom_v2.digest import canonical_json_bytes, digest_bytes, digest_json


ROOT = Path(__file__).resolve().parents[2]
PACKAGES = (
    "suanli_overleaf_http",
    "autopaper_http",
    "embedding_http",
    "kubernetes_http",
    "outline_http",
    "suanlicloud_http",
    "vm_ssh_http",
    "suanli_vm_ssh_http",
)


def test_contract_generator_is_clean() -> None:
    completed = subprocess.run(
        [sys.executable, "scripts/generate_service_contracts.py", "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("package_name", PACKAGES)
def test_service_package_exports_reference_real_io_contracts(package_name: str) -> None:
    package_dir = ROOT / "capability_services" / package_name
    manifest = _load(package_dir / "package.example.json")
    assert manifest["execution"] == {"kind": "container:http", "version": "1"}

    for export in manifest["action_exports"]:
        action = export["action_descriptor_ref"]["resource_id"].rsplit("/", 1)[-1]
        contract_ref = export["io_contract_ref"]
        contract_path = package_dir / "contracts" / f"{action}.io.json"
        contract = _load(contract_path)
        assert contract_ref["resource_id"] == content_uri(digest_bytes(canonical_json_bytes(contract)), f"{manifest['package_id']}/contracts/{action}")
        parsed = IoContract.model_validate(contract)
        assert (parsed.success_validator_ref is not None) == (action == "exec")

        for schema_ref in (parsed.input_schema_ref, parsed.output_schema_ref):
            assert schema_ref is not None
            digest = schema_ref.resource_id.rsplit("/", 1)[-1]
            # The file name is action-oriented; resolve by digest instead.
            matching = [
                path for path in (package_dir / "schemas").glob("*.json")
                if digest_bytes(canonical_json_bytes(_load(path))) == digest
            ]
            assert matching, (package_name, action, digest)
            validate_schema(_load(matching[0]))


@pytest.mark.parametrize("package_name", PACKAGES)
def test_service_contract_artifacts_are_deterministic(package_name: str) -> None:
    package_dir = ROOT / "capability_services" / package_name
    manifest = _load(package_dir / "package.example.json")
    for export in manifest["action_exports"]:
        action = export["action_descriptor_ref"]["resource_id"].rsplit("/", 1)[-1]
        contract = _load(package_dir / "contracts" / f"{action}.io.json")
        assert contract["schema_version"] == "io.v1"
        assert (contract["success_validator_ref"] is not None) == (action == "exec")
        descriptor = _load(package_dir / "descriptors" / f"{action}.json")
        validate_action_descriptor(descriptor)
        assert export["action_descriptor_ref"]["version_or_digest"] == digest_json(descriptor, domain="loom/action-descriptor/v1")


def test_suanli_resource_effect_descriptor_sources_are_valid() -> None:
    descriptor_dir = ROOT / "capability_services" / "suanlicloud_http" / "descriptors"
    run_descriptor = _load(descriptor_dir / "instances-run.json")
    validate_action_descriptor(run_descriptor)
    effect = run_descriptor["resource_effects"][0]
    assert effect["identity_path"] == "result.Instances[].InstanceId"
    assert effect["access_requirements"][0]["capability_kind"] == "compute.vm.remote_shell"
    assert effect["cleanup"]["action_ref"]["resource_id"] == "grip://loom.local/workspaces/workspace-default/actions/suanlicloud/instances-terminate"
    assert effect["readiness"]["action_ref"]["resource_id"] == "grip://loom.local/workspaces/workspace-default/actions/suanlicloud/instances-list"
    assert effect["readiness"]["result_path"] == "result.Instances[]"
    assert effect["readiness"]["conditions"][0]["value"] == "running"

    ssh_descriptor = _load(
        ROOT / "capability_services" / "suanli_vm_ssh_http" / "descriptors" / "exec.json"
    )
    assert ssh_descriptor["capability_kinds"] == ["compute.vm.remote_shell"]
    config_schema = _load(ROOT / "capability_services" / "suanli_vm_ssh_http" / "schemas" / "instance-config.json")
    validate_schema(config_schema)
    assert ssh_descriptor["instance_config_schema_ref"]["resource_id"] == content_uri(digest_bytes(canonical_json_bytes(config_schema)), "suanli-vm-ssh-http/schemas/instance-config")


def test_suanli_ssh_schemas_use_supported_io_subset():
    directory = ROOT / "capability_services" / "suanli_vm_ssh_http"
    contract = _load(directory / "contracts/exec.io.json")
    IoContract.model_validate(contract)
    manifest = _load(directory / "package.example.json")
    validator = directory / "validators/exec-success.py"
    assert contract["success_validator_ref"]["resource_id"] == content_uri(digest_bytes(validator.read_bytes()), "suanli-vm-ssh-http/validators/exec-success")
    assert manifest["action_exports"][0]["io_contract_ref"]["resource_id"] == content_uri(digest_bytes(canonical_json_bytes(contract)), "suanli-vm-ssh-http/contracts/exec")
    for direction in ("input", "output"):
        schema = _load(directory / "schemas" / f"exec-{direction}.json")
        validate_schema(schema)
        assert contract[f"{direction}_schema_ref"]["resource_id"] == content_uri(digest_bytes(canonical_json_bytes(schema)), f"suanli-vm-ssh-http/schemas/exec-{direction}")


def test_suanli_readiness_schema_matches_provider_response():
    directory = ROOT / "capability_services/suanlicloud_http/schemas"
    assert not validate(_load(directory / "instances-list-input.json"), {"InstanceIds": ["vm-test"], "Filters": []})
    payload = {"action": "instances_list", "result": {"Instances": [{"InstanceId": "vm-test", "State": "running", "Ip": "192.0.2.1"}], "PageNumber": 1, "PageSize": 20, "TotalCount": 1}}
    assert not validate(_load(directory / "instances-list-output.json"), payload)
    status = _load(directory / "instances-status-output.json")
    assert "Ip" not in status["properties"]["result"]["properties"]["InstanceStatuses"]["items"]["properties"]


@pytest.mark.parametrize("exit_code,timed_out,expected", [(0, False, "pass"), (1, False, "fail"), (None, True, "fail"), (0, True, "fail"), (False, False, "fail")])
def test_suanli_ssh_success_validator(exit_code, timed_out, expected):
    validator = ROOT / "capability_services/suanli_vm_ssh_http/validators/exec-success.py"
    completed = subprocess.run([sys.executable, str(validator)], input=json.dumps({"exit_code": exit_code, "timed_out": timed_out}), text=True, capture_output=True, check=True)
    assert json.loads(completed.stdout)["result"] == expected
