#!/usr/bin/env python3
"""Generate content-addressed IoContracts for the HTTP service packages.

The JSON files under each capability service are the reviewable contract
source.  This command only derives schema/contract files and replaces the
``io_contract_ref`` placeholders in the example manifest; it never derives
descriptor digests or image digests.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from loom_v2.contracts.io_schema import validate_schema
from loom_v2.contracts.types import IoContract
from loom_v2.digest import canonical_json_bytes, digest_bytes


ROOT = Path(__file__).resolve().parents[1]


def obj(required: list[str] | None = None, **properties: Any) -> dict[str, Any]:
    value: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        value["required"] = required
    return value


def string(*, const: str | None = None, enum: list[str] | None = None) -> dict[str, Any]:
    value: dict[str, Any] = {"type": "string"}
    if const is not None:
        value["const"] = const
    if enum is not None:
        value["enum"] = enum
    return value


def array(item: dict[str, Any]) -> dict[str, Any]:
    return {"type": "array", "items": item}


def k8s_definitions() -> dict[str, dict[str, Any]]:
    empty = obj()
    status = string(enum=["auth_required", "authenticated", "auth_failed"])
    return {
        "auth-start": {"input": empty, "output": obj(["status"], status=status)},
        "auth-status": {"input": empty, "output": obj(["status"], status=status)},
        "resources-list": {
            "input": obj(resource=string(enum=["pods", "pvc", "jobs", "vcjobs"])),
            "output": obj(["status", "resource"], status=string(const="ok"), resource=obj()),
        },
        "resources-get": {
            "input": obj(["resource", "name"], resource=string(enum=["pods", "pvc", "jobs", "vcjobs"]), name=string()),
            "output": obj(["status", "resource"], status=string(const="ok"), resource=obj()),
        },
        "jobs-submit": {
            "input": obj(["manifest"], manifest=obj()),
            "output": obj(["status", "name"], status=string(const="submitted"), name=string()),
        },
        "jobs-status": {
            "input": obj(["name"], name=string()),
            "output": obj(["status", "job"], status=string(const="ok"), job=obj()),
        },
        "jobs-logs": {
            "input": obj(["pod_name"], pod_name=string()),
            "output": obj(["status", "pod_name", "logs"], status=string(const="ok"), pod_name=string(), logs=string()),
        },
        "jobs-delete": {
            "input": obj(["name"], name=string()),
            "output": obj(["status", "name"], status=string(const="deleted"), name=string()),
        },
    }


def outline_definitions() -> dict[str, dict[str, Any]]:
    empty = obj()
    by_operation = {
        "collections-list": empty,
        "collections-info": obj(["id"], id=string()),
        "collections-documents": obj(["id"], id=string()),
        "documents-list": empty,
        "documents-info": obj(["documentId"], documentId=string()),
        "documents-documents": obj(["documentId"], documentId=string()),
        "documents-search-titles": obj(["query"], query=string()),
        "documents-search": obj(["query"], query=string()),
        "documents-create": obj(["collectionId", "title"], collectionId=string(), title=string()),
        "documents-update": obj(["id"], id=string()),
        "documents-archive": obj(["id"], id=string()),
        "documents-restore": obj(["id"], id=string()),
    }
    result: dict[str, dict[str, Any]] = {}
    for operation, input_schema in by_operation.items():
        internal_name = operation.replace("-", "_")
        result[operation] = {
            "input": input_schema,
            "output": obj(["operation", "result"], operation=string(const=internal_name), result=obj()),
        }
    result["auth-start"] = {"input": empty, "output": obj(["status"], status=string(const="authenticated"))}
    result["auth-status"] = {"input": empty, "output": obj(["status"], status=string(const="authenticated"))}
    return result


def suanli_definitions() -> dict[str, dict[str, Any]]:
    empty = obj()
    filters = array(obj())
    instance_ids = array(string())
    provider_results = {
        "regions-list": (empty, ["Regions"], {"Regions": array(obj())}),
        "zones-list": (obj(["RegionId"], RegionId=string()), ["Zones"], {"Zones": array(obj())}),
        "nodes-list": (obj(["RegionId", "Filters"], RegionId=string(), Filters=filters), ["Nodes"], {"Nodes": array(obj())}),
        "instances-list": (
            obj(["ProjectId", "InstanceIds", "Filters", "PageSpec"], ProjectId=string(), InstanceIds=instance_ids, Filters=filters, PageSpec=obj()),
            ["Instances", "PageMeta"],
            {"Instances": array(obj()), "PageMeta": obj()},
        ),
        "instances-status": (
            obj(["InstanceIds", "Filters", "IncludeAllInstances"], InstanceIds=instance_ids, Filters=filters, IncludeAllInstances={"type": "boolean"}),
            ["InstanceStatuses"],
            {"InstanceStatuses": array(obj())},
        ),
        "images-list": (obj(ZoneId=string()), ["Images"], {"Images": array(obj())}),
        "instances-run": (
            obj(
                ["ProjectId", "RegionId", "ZoneId", "Count", "Cpu", "Mem", "DiskSize", "ImageId", "Name", "TagSpecifications", "ClientToken"],
                ProjectId=string(), RegionId=string(), ZoneId=string(), Count={"type": "integer"}, Cpu={"type": "integer"},
                Mem={"type": "integer"}, DiskSize={"type": "integer"}, ImageId=string(), Name=string(),
                TagSpecifications=array(obj()), ClientToken=string(),
            ),
            ["Instances"],
            {"Instances": array(obj())},
        ),
    }
    for operation in ("instances-start", "instances-stop", "instances-reboot", "instances-shutdown", "instances-terminate"):
        provider_results[operation] = (obj(["InstanceIds"], InstanceIds=instance_ids), ["StateTransitions"], {"StateTransitions": array(obj())})

    result: dict[str, dict[str, Any]] = {}
    for operation, (input_schema, required, properties) in provider_results.items():
        internal_name = operation.replace("-", "_")
        result[operation] = {
            "input": input_schema,
            "output": obj(
                ["operation", "result"],
                operation=string(const=internal_name),
                result=obj(required, **properties),
            ),
        }
    result["auth-start"] = {"input": empty, "output": obj(["status"], status=string(const="authenticated"))}
    result["auth-status"] = {"input": empty, "output": obj(["status"], status=string(const="authenticated"))}
    return result


DEFINITIONS = {
    "kubernetes_http": k8s_definitions,
    "outline_http": outline_definitions,
    "suanlicloud_http": suanli_definitions,
}


def content_ref(value: Any) -> dict[str, str]:
    digest = digest_bytes(canonical_json_bytes(value))
    return {"resource_id": f"content://sha256/{digest}", "identity_criterion": "content_digest"}


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def generate(package_dir: Path, definition_factory: Any, *, check: bool = False) -> None:
    definitions = definition_factory()
    manifest_path = package_dir / "package.example.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    exports = {item["capability_descriptor_ref"]["resource_id"].rsplit("/", 1)[-1]: item for item in manifest["capability_exports"]}
    expected_paths: set[Path] = set()
    for operation, definition in definitions.items():
        export = exports.get(operation)
        # Authentication paths are part of the generic container-http
        # handshake.  Only Kubernetes currently exports them as ordinary
        # capabilities; other gateways keep them private to the runtime.
        if export is None:
            continue
        input_schema = definition["input"]
        output_schema = definition["output"]
        validate_schema(input_schema)
        validate_schema(output_schema)
        input_ref = content_ref(input_schema)
        output_ref = content_ref(output_schema)
        contract = {
            "schema_version": "io.v1",
            "input_schema_ref": input_ref,
            "output_schema_ref": output_ref,
            "success_semantics": None,
            "success_validator_ref": None,
        }
        contract_ref = content_ref(contract)
        schema_paths = [
            package_dir / "schemas" / f"{operation}-input.json",
            package_dir / "schemas" / f"{operation}-output.json",
        ]
        contract_path = package_dir / "contracts" / f"{operation}.io.json"
        expected_paths.update(schema_paths)
        expected_paths.add(contract_path)
        if not check:
            write_json(schema_paths[0], input_schema)
            write_json(schema_paths[1], output_schema)
            write_json(contract_path, contract)
        else:
            actual_input = json.loads(schema_paths[0].read_text(encoding="utf-8"))
            actual_output = json.loads(schema_paths[1].read_text(encoding="utf-8"))
            actual_contract = json.loads(contract_path.read_text(encoding="utf-8"))
            if actual_input != input_schema or actual_output != output_schema:
                raise SystemExit(f"generated schema is stale: {package_dir.name}:{operation}")
            IoContract.model_validate(actual_contract)
            if actual_contract != contract:
                raise SystemExit(f"generated IoContract is stale: {package_dir.name}:{operation}")
        export["io_contract_ref"] = contract_ref

    if not check:
        write_json(manifest_path, manifest)
    else:
        if any(not path.is_file() for path in expected_paths):
            missing = ", ".join(str(path) for path in sorted(expected_paths) if not path.is_file())
            raise SystemExit(f"generated contract artifact missing: {missing}")
        actual_paths = {
            *package_dir.joinpath("schemas").glob("*.json"),
            *package_dir.joinpath("contracts").glob("*.json"),
        }
        extras = actual_paths - expected_paths
        if extras:
            raise SystemExit(f"stale generated contract artifact: {', '.join(str(path) for path in sorted(extras))}")
        actual_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for item in actual_manifest["capability_exports"]:
            operation = item["capability_descriptor_ref"]["resource_id"].rsplit("/", 1)[-1]
            expected = exports[operation]["io_contract_ref"]
            if item["io_contract_ref"] != expected:
                raise SystemExit(f"manifest IoContract ref is stale: {package_dir.name}:{operation}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="verify generated files and manifest refs")
    args = parser.parse_args()
    for package_name, factory in DEFINITIONS.items():
        generate(ROOT / "capability_services" / package_name, factory, check=args.check)


if __name__ == "__main__":
    main()
