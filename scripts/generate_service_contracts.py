#!/usr/bin/env python3
"""Generate content-addressed IoContracts for the HTTP service packages.

The JSON files under each capability service are the reviewable contract
source.  This command only derives schema/contract files and replaces the
``io_contract_ref`` placeholders in the example manifest. Descriptor templates get their source digest; publication
resolves dependency references and obtains final descriptor refs from Observer.
Image digests are always supplied by the publisher.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from loom_orchestration.naming import content_uri
from loom_v2.contracts.io_schema import validate_schema
from loom_v2.contracts.types import IoContract
from loom_v2.digest import canonical_json_bytes, digest_bytes, digest_json


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
    return {
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
    by_action = {
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
    for action, input_schema in by_action.items():
        internal_name = action.replace("-", "_")
        result[action] = {
            "input": input_schema,
            "output": obj(["action", "result"], action=string(const=internal_name), result=obj()),
        }
    return result


def suanli_definitions() -> dict[str, dict[str, Any]]:
    empty = obj()
    filters = array(obj())
    instance_ids = array(string())
    instance_identity = obj(
        ["InstanceId"],
        InstanceId=string(),
        State=string(),
        Ip={"type": ["string", "null"]},
        Ips=array(string()),
    )
    instance_status = obj(
        ["InstanceId", "State"],
        InstanceId=string(),
        State=string(),
        Reason=string(),
    )
    provider_results = {
        "regions-list": (empty, ["Regions"], {"Regions": array(obj())}),
        "zones-list": (obj(["RegionId"], RegionId=string()), ["Zones"], {"Zones": array(obj())}),
        "nodes-list": (obj(["RegionId", "Filters"], RegionId=string(), Filters=filters), ["Nodes"], {"Nodes": array(obj())}),
        "instances-list": (
            obj(ProjectId=string(), InstanceIds=instance_ids, Filters=filters, PageNumber={"type": "integer"}, PageSize={"type": "integer"}),
            ["Instances"],
            {"Instances": array(instance_identity), "PageNumber": {"type": "integer"}, "PageSize": {"type": "integer"}, "TotalCount": {"type": "integer"}},
        ),
        "instances-status": (
            obj(["InstanceIds", "Filters", "IncludeAllInstances"], InstanceIds=instance_ids, Filters=filters, IncludeAllInstances={"type": "boolean"}),
            ["InstanceStatuses"],
            {"InstanceStatuses": array(instance_status)},
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
            {"Instances": array(instance_identity)},
        ),
    }
    for action in ("instances-start", "instances-stop", "instances-reboot", "instances-shutdown", "instances-terminate"):
        provider_results[action] = (obj(["InstanceIds"], InstanceIds=instance_ids), ["StateTransitions"], {"StateTransitions": array(obj())})

    result: dict[str, dict[str, Any]] = {}
    for action, (input_schema, required, properties) in provider_results.items():
        internal_name = action.replace("-", "_")
        result[action] = {
            "input": input_schema,
            "output": obj(
                ["action", "result"],
                action=string(const=internal_name),
                result=obj(required, **properties),
            ),
        }
    return result


def volcano_definitions() -> dict[str, dict[str, Any]]:
    namespace = {"type": "string", "minLength": 1, "maxLength": 63}
    text = {"type": "string", "maxLength": 256}
    count = {"type": "integer", "minimum": 0}
    filters = {"namespace": namespace, "status": text, "search": text}
    inputs = {
        "namespaces-list": obj(),
        "queues-list": obj(state=text, search=text),
        "podgroups-list": obj(**filters),
        "pods-list": obj(**filters),
        "jobs-list": obj(**filters, queue=text),
        "gpu-usage": obj(namespace=namespace, resource_name={
            "type": "string", "minLength": 3, "maxLength": 253,
        }),
    }
    metrics = {key: count for key in ("allocated", "running_allocated", "unscheduled_requested")}
    gpu_pod = obj(["namespace", "name", "node_name", "phase", "gpu_requests", "scheduled"],
                  namespace=string(), name=string(), node_name={"type": ["string", "null"]},
                  phase=string(), gpu_requests=count, scheduled={"type": "boolean"})
    namespace_totals = obj(["namespace", "gpu_pods", *metrics], namespace=string(), gpu_pods=count, **metrics)
    gpu_output = obj(
        ["status", "complete", "metric", "collected_at", "namespace", "resource_name", "pod_count",
         "terminal_pods", *metrics, "by_namespace", "gpu_pods"],
        status=string(const="ok"), complete={"const": True},
        metric=string(const="scheduled_pod_requests"), collected_at=string(), namespace=string(),
        resource_name=string(), pod_count=count, terminal_pods=count, **metrics,
        by_namespace=array(namespace_totals), gpu_pods=array(gpu_pod),
    )
    result = {}
    for action, input_schema in inputs.items():
        result[action] = {
            "input": input_schema,
            "output": gpu_output if action == "gpu-usage" else obj(
                ["status", "items", "total_count", "complete"], status=string(const="ok"),
                items=array(obj()), total_count=count, complete={"const": True}),
        }
    return result


def grafana_definitions() -> dict[str, dict[str, Any]]:
    text = {"type": "string", "minLength": 1, "maxLength": 32768}
    identity = {"type": "string", "minLength": 1, "maxLength": 128}
    at = {"type": ["string", "number"]}
    boolean = {"type": "boolean"}
    strings = array(string())
    gpu_metric = obj(["name", "node_label", "index_label", "unit"],
                     name=string(), node_label=string(), index_label=string(), uuid_label=string(),
                     unit=string(enum=["percent", "ratio"]))
    query_output = obj(
        ["status", "complete", "datasource_uid", "queried_at", "query", "parameters",
         "result_type", "result", "warnings", "infos"],
        status=string(const="ok"), complete=boolean, datasource_uid=identity,
        queried_at=string(), query=text, parameters=obj(),
        result_type=string(enum=["vector", "matrix", "scalar", "string"]),
        result={"type": "array", "items": {}}, warnings=strings, infos=strings,
    )
    sample = obj(
        ["hostname", "gpu_index", "gpu_uuid", "utilization_percent", "sampled_at", "labels"],
        hostname=string(), gpu_index={"type": "integer", "minimum": 0},
        gpu_uuid={"type": ["string", "null"]},
        utilization_percent={"type": "number", "minimum": 0, "maximum": 100},
        sampled_at=string(), labels=obj(),
    )
    return {
        "datasources-list": {
            "input": obj(),
            "output": obj(["status", "items", "complete"], status=string(const="ok"),
                          items=array(obj(["uid", "name", "type", "is_default"], uid=identity,
                                          name=string(), type=string(), is_default=boolean)), complete={"const": True}),
        },
        "dashboards-list": {
            "input": obj(search={"type": "string", "maxLength": 256}),
            "output": obj(["status", "items", "total_count", "complete"], status=string(const="ok"),
                          items=array(obj(["uid", "title"], uid=identity, title=string())),
                          total_count={"type": "integer", "minimum": 0}, complete={"const": True}),
        },
        "dashboard-get": {
            "input": obj(["uid"], uid=identity),
            "output": obj(["status", "dashboard"], status=string(const="ok"), dashboard=obj()),
        },
        "metrics-query": {
            "input": obj(["query"], query=text, datasource_uid=identity, time=at),
            "output": query_output,
        },
        "metrics-query-range": {
            "input": obj(["query", "start", "end", "step"], query=text, start=at, end=at,
                         step={"type": "integer", "minimum": 1}, datasource_uid=identity),
            "output": query_output,
        },
        "gpu-utilization": {
            "input": obj(["hostname"], hostname={"type": "string", "minLength": 1, "maxLength": 253},
                         gpu_index={"type": "integer", "minimum": 0}, time=at,
                         datasource_uid=identity, gpu_metric=gpu_metric),
            "output": obj(["status", "complete", "hostname", "unit", "metric", "datasource_uid",
                           "queried_at", "query_time", "samples", "warnings", "infos"],
                          status=string(enum=["ok", "no_data"]), complete=boolean, hostname=string(),
                          unit=string(const="percent"), metric=string(), datasource_uid=identity,
                          queried_at=string(), query_time=string(), samples=array(sample), warnings=strings, infos=strings),
        },
    }


DEFINITIONS = {
    "kubernetes_http": k8s_definitions,
    "volcano_http": volcano_definitions,
    "grafana_http": grafana_definitions,
    "outline_http": outline_definitions,
    "suanlicloud_http": suanli_definitions,
    "vm_ssh_http": None,
    "suanli_vm_ssh_http": None,
}


def content_ref(value: Any, name: str) -> dict[str, str]:
    digest = digest_bytes(canonical_json_bytes(value))
    return {"resource_id": content_uri(digest, name), "identity_criterion": "content_digest"}


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def generate(package_dir: Path, definition_factory: Any, *, check: bool = False) -> None:
    definitions = definition_factory() if definition_factory else {
        "exec": {direction: json.loads((package_dir / "schemas" / f"exec-{direction}.json").read_text()) for direction in ("input", "output")}
    }
    manifest_path = package_dir / "package.example.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    exports = {item["action_descriptor_ref"]["resource_id"].rsplit("/", 1)[-1]: item for item in manifest["action_exports"]}
    if set(exports) != set(definitions):
        raise SystemExit(f"export definitions do not match manifest: {package_dir.name}")
    expected_paths: set[Path] = set()
    for action, definition in definitions.items():
        export = exports[action]
        input_schema = definition["input"]
        output_schema = definition["output"]
        validate_schema(input_schema)
        validate_schema(output_schema)
        prefix = manifest["package_id"]
        input_ref = content_ref(input_schema, f"{prefix}/schemas/{action}-input")
        output_ref = content_ref(output_schema, f"{prefix}/schemas/{action}-output")
        contract = {
            "schema_version": "io.v1",
            "input_schema_ref": input_ref,
            "output_schema_ref": output_ref,
            "success_semantics": None,
            "success_validator_ref": None,
        }
        validator = package_dir / "validators" / f"{action}-success.py"
        if validator.exists():
            contract["success_semantics"] = "Command exits with code zero and does not time out."
            contract["success_validator_ref"] = {"resource_id": content_uri(digest_bytes(validator.read_bytes()), f"{prefix}/validators/{action}-success"), "identity_criterion": "content_digest"}
        contract_ref = content_ref(contract, f"{prefix}/contracts/{action}")
        schema_paths = [
            package_dir / "schemas" / f"{action}-input.json",
            package_dir / "schemas" / f"{action}-output.json",
        ]
        contract_path = package_dir / "contracts" / f"{action}.io.json"
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
                raise SystemExit(f"generated schema is stale: {package_dir.name}:{action}")
            IoContract.model_validate(actual_contract)
            if actual_contract != contract:
                raise SystemExit(f"generated IoContract is stale: {package_dir.name}:{action}")
        export["io_contract_ref"] = contract_ref
        descriptor_path = package_dir / "descriptors" / f"{action}.json"
        descriptor = json.loads(descriptor_path.read_text())
        config_path = package_dir / "schemas" / "instance-config.json"
        if config_path.exists():
            descriptor["instance_config_schema_ref"] = content_ref(json.loads(config_path.read_text()), f"{prefix}/schemas/instance-config")
            expected_paths.add(config_path)
        if check:
            if json.loads(descriptor_path.read_text()) != descriptor:
                raise SystemExit(f"stale descriptor: {descriptor_path}")
        else:
            write_json(descriptor_path, descriptor)
        export["action_descriptor_ref"] = {
            "resource_id": descriptor["resource_id"],
            "version_or_digest": digest_json(descriptor, domain="loom/action-descriptor/v1"),
            "identity_criterion": "descriptor_digest",
        }

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
        for item in actual_manifest["action_exports"]:
            action = item["action_descriptor_ref"]["resource_id"].rsplit("/", 1)[-1]
            expected = exports[action]["io_contract_ref"]
            if item["action_descriptor_ref"] != exports[action]["action_descriptor_ref"]:
                raise SystemExit(f"manifest descriptor ref is stale: {package_dir.name}:{action}")
            if item["io_contract_ref"] != expected:
                raise SystemExit(f"manifest IoContract ref is stale: {package_dir.name}:{action}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="verify generated files and manifest refs")
    args = parser.parse_args()
    for package_name, factory in DEFINITIONS.items():
        generate(ROOT / "capability_services" / package_name, factory, check=args.check)


if __name__ == "__main__":
    main()
