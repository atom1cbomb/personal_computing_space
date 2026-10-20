"""Builder for the existing ``container:python_orchestrator/1`` package."""

from __future__ import annotations

from typing import Any

from .contracts import ResourceRef, ensure_sha256


class OrchestratorPackage:
    def __init__(
        self,
        *,
        program_content_ref: ResourceRef,
        allowed_node_package_refs: list[ResourceRef],
        max_nodes: int = 32,
        max_live_nodes: int = 8,
        package_id: str = "loom-orchestrator",
        package_version: str = "1",
        action_descriptor_ref: ResourceRef,
        io_contract_ref: ResourceRef,
        replay_safety: str = "DeterministicByEventLog",
    ) -> None:
        if max_nodes < 1 or max_live_nodes < 1 or max_live_nodes > max_nodes:
            raise ValueError("orchestration_budget_invalid")
        self.program_content_ref = ensure_sha256(program_content_ref)
        self.allowed_node_package_refs = [ensure_sha256(item) for item in allowed_node_package_refs]
        self.action_descriptor_ref = ensure_sha256(action_descriptor_ref, descriptor=True)
        self.io_contract_ref = ensure_sha256(io_contract_ref)
        self.max_nodes = max_nodes
        self.max_live_nodes = max_live_nodes
        self.package_id = package_id
        self.package_version = package_version
        self.replay_safety = replay_safety

    def manifest(self) -> dict[str, Any]:
        return {
            "package_id": self.package_id,
            "package_version": self.package_version,
            "package_type": "function",
            "execution": {"kind": "container:python_orchestrator", "version": "1"},
            "action_exports": [{
                "action_descriptor_ref": self.action_descriptor_ref.model_dump(mode="json"),
                "io_contract_ref": self.io_contract_ref.model_dump(mode="json"),
                "effect_class": "Known",
                "permissions": [],
                "replay_safety": self.replay_safety,
                "runtime_binding": {},
            }],
            "body": {
                "program_content_ref": self.program_content_ref.model_dump(mode="json"),
                "allowed_node_package_refs": [item.model_dump(mode="json") for item in self.allowed_node_package_refs],
                "max_nodes": self.max_nodes,
                "max_live_nodes": self.max_live_nodes,
                "captures_run_state": False,
                "captured_secret_refs": [],
                "captured_path_refs": [],
            },
        }


__all__ = ["OrchestratorPackage"]
