from __future__ import annotations

import asyncio
import hashlib

from benchmarks.composite_heterogeneous_scheduling.run_composite import build_orchestration_source
from loom_v2.contracts.types import ResourceRef
from loom_v2.driver.orchestrator import DockerOrchestrationExecutor


def _ref(name: str) -> ResourceRef:
    digest = hashlib.sha256(name.encode()).hexdigest()
    return ResourceRef(resource_id=f"content://sha256/{digest}")


def test_orchestration_emits_three_stages_then_one_aggregate_node() -> None:
    packages = {
        "preprocess": (_ref("pre-package"), _ref("pre-descriptor")),
        "detect": (_ref("detect-package"), _ref("detect-descriptor")),
        "risk": (_ref("risk-package"), _ref("risk-descriptor")),
        "aggregate": (_ref("aggregate-package"), _ref("aggregate-descriptor")),
    }
    source = build_orchestration_source(packages)
    input_ref = _ref("parent-input")
    image_refs = [_ref("image-1"), _ref("image-2")]
    expected_ref = _ref("expected")
    emitted: list[tuple[ResourceRef, ResourceRef, list[ResourceRef]]] = []
    results: dict[str, ResourceRef] = {}

    def emit_node(package_ref: ResourceRef, descriptor_ref: ResourceRef, refs: list[ResourceRef]) -> str:
        handle = f"handle-{len(emitted) + 1}"
        emitted.append((package_ref, descriptor_ref, refs))
        results[handle] = _ref(f"result-{len(emitted)}")
        return handle

    final_ref = asyncio.run(
        DockerOrchestrationExecutor(image="python:3.12-slim").run(
            source.encode(),
            input_ref,
            read_json=lambda ref: {
                "request_refs": [item.model_dump(mode="json") for item in image_refs],
                "expected_ref": expected_ref.model_dump(mode="json"),
            },
            emit_node=emit_node,
            result=lambda handle: results[handle],
        )
    )

    assert [item[0] for item in emitted] == [
        packages["preprocess"][0],
        packages["preprocess"][0],
        packages["detect"][0],
        packages["detect"][0],
        packages["risk"][0],
        packages["risk"][0],
        packages["aggregate"][0],
    ]
    assert emitted[-1][2] == [expected_ref, _ref("result-5"), _ref("result-6")]
    assert final_ref == _ref("result-7")


def test_formal_node_limits_are_derived_from_image_count() -> None:
    from benchmarks.composite_heterogeneous_scheduling.run_composite import locality_compute_spec, node_limits

    assert node_limits(100) == {"max_nodes": 301, "max_live_nodes": 100}
    assert node_limits(3) == {"max_nodes": 10, "max_live_nodes": 3}
    assert locality_compute_spec("slave-c") == {
        "operation_ref": "loom://composite-inspection",
        "requirements": [{"key": "loom.data.locality.v1", "value": "slave-c", "view": "systems"}],
    }
