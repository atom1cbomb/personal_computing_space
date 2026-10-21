"""The intentionally narrow protocol exposed inside an orchestration sandbox."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any, Protocol

from .contracts import ResourceRef


class OrchestrationContext(Protocol):
    async def sleep(self, seconds: float) -> None: ...

    async def read_json(self, ref: ResourceRef) -> Any: ...

    async def materialize_json(self, *, step_id: str, value: Any) -> ResourceRef: ...

    async def register_resource(
        self,
        *,
        step_id: str,
        mcue_ref: str,
        identity: str,
        package_ref: ResourceRef | None = None,
        managed_by_ref: ResourceRef | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> ResourceRef: ...

    async def ensure_activation(
        self,
        *,
        step_id: str,
        package_ref: ResourceRef,
        target_resource_ref: ResourceRef,
        instance_name: str,
        instance_config: dict[str, Any],
        credential_profile: str | None = None,
        requires_authentication: bool,
    ) -> ResourceRef: ...

    async def wait_activation(
        self, *, step_id: str, activation_ref: ResourceRef, ready_state: str
    ) -> dict[str, Any]: ...

    async def defer_cleanup(
        self,
        *,
        step_id: str,
        resource_ref: ResourceRef,
        cleanup_action_ref: ResourceRef,
        input_mapping: dict[str, Any],
    ) -> None: ...

    def emit_node(
        self,
        step_id: str,
        package_ref: ResourceRef,
        action_descriptor_ref: ResourceRef,
        input_refs: list[ResourceRef],
        *,
        target_resource_ref: ResourceRef | None = None,
        activation_ref: ResourceRef | None = None,
    ) -> str: ...

    async def result(self, handle: str) -> ResourceRef: ...


async def orchestrate(ctx: OrchestrationContext, input_ref: ResourceRef) -> ResourceRef:
    """Typing-only entry point used by generated programs and documentation."""
    raise NotImplementedError


class ProgramSource:
    """Validated UTF-8 source for the single sandbox entry point."""

    def __init__(self, source: str) -> None:
        if not isinstance(source, str) or not source.strip():
            raise ValueError("program_source_required")
        try:
            tree = ast.parse(source, filename="orchestration.py")
        except SyntaxError as exc:
            raise ValueError("program_source_invalid") from exc
        entries = [node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "orchestrate"]
        if len(entries) != 1:
            raise ValueError("orchestrate_function_required")
        entry = entries[0]
        if len(entry.args.posonlyargs) or len(entry.args.args) != 2 or entry.args.vararg or entry.args.kwarg or entry.args.kwonlyargs:
            raise ValueError("orchestrate_signature_invalid")
        if [arg.arg for arg in entry.args.args] != ["ctx", "input_ref"]:
            raise ValueError("orchestrate_signature_invalid")
        self.source = source

    @classmethod
    def from_file(cls, path: str | Path) -> "ProgramSource":
        return cls(Path(path).read_text(encoding="utf-8"))

    def as_bytes(self) -> bytes:
        return self.source.encode("utf-8")

    def content_upload(self, *, media_type: str = "text/x-python") -> dict[str, Any]:
        return {"content": self.source, "media_type": media_type}


__all__ = ["OrchestrationContext", "ProgramSource", "orchestrate"]
