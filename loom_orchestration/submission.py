"""One-shot complete closure construction."""

from __future__ import annotations

from typing import Any

from .contracts import ClosureContract, ResourceRef, TaskClosure


class SubmissionBuilder:
    def __init__(
        self,
        *,
        goal: str,
        body: TaskClosure | dict[str, Any],
        orchestrator_package: ResourceRef | dict[str, Any],
        input_binding: ResourceRef | dict[str, Any] | None = None,
        resource_budget: dict[str, Any] | None = None,
        workspace_id: str = "workspace-default",
        allowed_effects: list[str] | None = None,
        recovery_policy: dict[str, Any] | None = None,
    ) -> None:
        self.goal = goal
        if isinstance(body, TaskClosure):
            self.body = body
        elif hasattr(body, "model_dump"):
            self.body = TaskClosure.model_validate(body.model_dump(mode="json"))
        else:
            self.body = TaskClosure.model_validate(body)
        if isinstance(orchestrator_package, ResourceRef):
            self.orchestrator_package = orchestrator_package
        elif hasattr(orchestrator_package, "model_dump"):
            self.orchestrator_package = ResourceRef.model_validate(orchestrator_package.model_dump(mode="json"))
        else:
            self.orchestrator_package = ResourceRef.model_validate(orchestrator_package)
        if isinstance(input_binding, dict) and "input_ref" in input_binding:
            input_binding = input_binding["input_ref"]
        if hasattr(input_binding, "model_dump"):
            input_binding = input_binding.model_dump(mode="json")
        self.input_binding = ResourceRef.model_validate(input_binding) if input_binding is not None else None
        self.resource_budget = dict(resource_budget or {})
        self.workspace_id = workspace_id
        self.allowed_effects = list(allowed_effects or [])
        self.recovery_policy = dict(recovery_policy or {})

    def build(self, *, request_id: str, conversation_ref: str | None = None, closure_id: str | None = None) -> ClosureContract:
        closure_id = closure_id or request_id
        body = self.body.model_copy(deep=True)
        body.closure_id = closure_id
        body.program_systems = {
            **body.program_systems,
            "execution": {"kind": "container:python_orchestrator", "version": "1"},
            "package_ref": self.orchestrator_package.model_dump(mode="json"),
        }
        if self.input_binding is not None:
            operation_ref = body.program.get("operation_ref") or "loom://orchestrate"
            body.program = {**body.program, "operation_ref": operation_ref}
            body.node_input_bindings = [
                *body.node_input_bindings,
                {"node_id": operation_ref, "input_ref": self.input_binding.model_dump(mode="json")},
            ]
        return ClosureContract(
            closure_id=closure_id,
            goal=self.goal,
            workspace_id=self.workspace_id,
            origin_conversation_ref=conversation_ref or "",
            allowed_effects=self.allowed_effects,
            resource_budget=self.resource_budget,
            recovery_policy=self.recovery_policy,
            body=body,
        )


__all__ = ["SubmissionBuilder"]
