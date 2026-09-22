from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest

from loom_v2.contracts.io_schema import validate_schema
from loom_v2.contracts.types import IoContract
from loom_v2.digest import canonical_json_bytes, digest_bytes


ROOT = Path(__file__).resolve().parents[2]
PACKAGES = (
    "kubernetes_http",
    "outline_http",
    "suanlicloud_http",
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

    for export in manifest["capability_exports"]:
        operation = export["capability_descriptor_ref"]["resource_id"].rsplit("/", 1)[-1]
        contract_ref = export["io_contract_ref"]
        contract_path = package_dir / "contracts" / f"{operation}.io.json"
        contract = _load(contract_path)
        assert contract_ref["resource_id"] == f"content://sha256/{digest_bytes(canonical_json_bytes(contract))}"
        parsed = IoContract.model_validate(contract)
        assert parsed.success_semantics is None
        assert parsed.success_validator_ref is None

        for schema_ref in (parsed.input_schema_ref, parsed.output_schema_ref):
            assert schema_ref is not None
            digest = schema_ref.resource_id.rsplit("/", 1)[-1]
            # The file name is operation-oriented; resolve by digest instead.
            matching = [
                path for path in (package_dir / "schemas").glob("*.json")
                if digest_bytes(canonical_json_bytes(_load(path))) == digest
            ]
            assert matching, (package_name, operation, digest)
            validate_schema(_load(matching[0]))


@pytest.mark.parametrize("package_name", PACKAGES)
def test_service_contract_artifacts_are_deterministic(package_name: str) -> None:
    package_dir = ROOT / "capability_services" / package_name
    manifest = _load(package_dir / "package.example.json")
    for export in manifest["capability_exports"]:
        operation = export["capability_descriptor_ref"]["resource_id"].rsplit("/", 1)[-1]
        contract = _load(package_dir / "contracts" / f"{operation}.io.json")
        assert contract["schema_version"] == "io.v1"
        assert contract["success_semantics"] is None
        assert contract["success_validator_ref"] is None
