"""Observer-side activation command execution boundary.

The repository remains the authority for desired/actual state and fencing.
This module only coordinates already-selected local workers/Slaves and turns
their reports into the API's health-report list.
"""

from __future__ import annotations

from typing import Any

from loom_v2.contracts.types import (
    CapabilityHealthReport,
    CapabilityDeprovisionCommand,
    CapabilityPackageVersion,
    CapabilityProvisionCommand,
    ResourceRef,
)


async def _driver_fencing(repository: Any, workspace_id: str) -> dict[str, Any]:
    """Read the active Driver lease used only for Slave fencing metadata."""
    agents = await repository.list_agents(workspace_id, role="driver")
    driver = next(
        (item for item in agents if item.get("lease_state") == "active"),
        None,
    )
    if driver is None:
        raise RuntimeError("driver_unavailable")
    return {
        "driver_id": str(driver["agent_id"]),
        "driver_epoch": int(driver["epoch"]),
    }


async def execute_activation_targets(
    repository: Any,
    package: CapabilityPackageVersion,
    targets: list[Any],
    *,
    desired_state: str,
    workspace_id: str,
    workers: dict[str, Any],
    slaves: dict[str, Any],
    compute_binding: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
    instance_name: str = "default",
    target_resource_ref: ResourceRef | dict[str, Any] | None = None,
    instance_config: dict[str, Any] | None = None,
    credential_profile: str | None = None,
    slave_gateway: Any | None = None,
    expected_revision: int | None = None,
) -> list[dict[str, Any]]:
    """Execute one promote/deactivate command for each selected target.

    Target selection remains outside this helper. Embedded tests may use local
    Worker/Slave objects; production uses the Observer-owned Slave gateway.
    """

    if isinstance(target_resource_ref, dict):
        target_resource_ref = ResourceRef.model_validate(target_resource_ref)
    reports: list[dict[str, Any]] = []
    fencing = None if workers or slaves else await _driver_fencing(repository, workspace_id)
    for raw_target in targets:
        target = str(raw_target)
        action = "promote" if desired_state == "running" else "deactivate"
        requested_key = idempotency_key or f"{action}-{package.package_digest}-{target}"
        desired = await repository.set_capability_desired(
            package.version_ref,
            target,
            desired_state=desired_state,
            idempotency_key=requested_key,
            workspace_id=workspace_id,
            activation_closure_version_ref=(
                package.package_closure_version_ref if desired_state == "running" else ""
            ),
            compute_binding_ref=str((compute_binding or {}).get("binding_id") or ""),
            instance_name=instance_name,
            target_resource_ref=target_resource_ref,
            instance_config=instance_config,
            credential_profile=credential_profile,
            **({"expected_revision": expected_revision} if expected_revision is not None else {}),
        )
        command_values: dict[str, Any] = {
            "command_id": f"{'provision' if desired_state == 'running' else 'deprovision'}-{package.package_id}-{target}",
            "package_version_ref": package.version_ref,
            "package_digest": package.package_digest,
            "target_slave": target,
            "instance_name": instance_name,
            "target_resource_ref": desired.target_resource_ref,
            "instance_config": dict(desired.instance_config),
            "credential_profile": desired.credential_profile,
            "workspace_id": workspace_id,
            "idempotency_key": desired.last_idempotency_key,
            "activation_revision": desired.activation_revision,
        }
        if desired_state == "running":
            command_values.update(
                {
                    "activation_closure_version_ref": package.package_closure_version_ref,
                    "compute_binding": compute_binding,
                }
            )
            command = CapabilityProvisionCommand.model_validate(command_values)
        elif desired_state == "stopped":
            command = CapabilityDeprovisionCommand.model_validate(command_values)
        else:
            raise ValueError("unsupported_activation_state")
        try:
            worker = workers.get(target)
            slave = slaves.get(target)
            if worker is not None:
                report = (
                    await worker.provision(command=command, package=package)
                    if desired_state == "running"
                    else await worker.deprovision(command=command, package=package)
                )
            elif slave is not None:
                report = (
                    await slave.provision(command, package)
                    if desired_state == "running"
                    else await slave.deprovision(command, package)
                )
            elif slave_gateway is not None:
                path = "/worker/v1/provision" if desired_state == "running" else "/worker/v1/deprovision"
                response = await slave_gateway.forward(
                    target,
                    path,
                    {
                        "command": command.model_dump(mode="json"),
                        "package": package.model_dump(mode="json"),
                        **(fencing or {}),
                    },
                )
                report = CapabilityHealthReport.model_validate(
                    response.get("health_report") or response
                )
            else:
                raise RuntimeError("slave_not_found")
            await repository.record_capability_health(report)
            reports.append(report.model_dump(mode="json"))
        except (RuntimeError, ValueError) as exc:
            reports.append(
                {
                    "target_slave": target,
                    "activation_state": "failed",
                    "error": str(exc),
                }
            )
    return reports


async def execute_auth_targets(
    repository: Any,
    package: CapabilityPackageVersion,
    targets: list[Any],
    *,
    workspace_id: str,
    workers: dict[str, Any],
    slaves: dict[str, Any],
    approved_digest: str | None = None,
    instance_name: str = "default",
    slave_gateway: Any | None = None,
    activation_revision: int | None = None,
) -> list[dict[str, Any]]:
    """Start authentication on existing activations without changing lifecycle."""
    if approved_digest is not None and approved_digest.lower() != package.package_digest.lower():
        raise ValueError("capability_package_digest_mismatch")
    reports: list[dict[str, Any]] = []
    fencing = None if workers or slaves else await _driver_fencing(repository, workspace_id)
    for raw_target in targets:
        target = str(raw_target)
        try:
            worker = workers.get(target)
            slave = slaves.get(target)
            if worker is not None:
                report = await worker.auth_start(
                    package=package,
                    target_slave=target,
                    workspace_id=workspace_id,
                    instance_name=instance_name,
                    **({"activation_revision": activation_revision} if activation_revision is not None else {}),
                )
            elif slave is not None:
                report = await slave.auth_start(package, instance_name=instance_name, activation_revision=activation_revision)
            elif slave_gateway is not None:
                response = await slave_gateway.forward(
                    target,
                    "/worker/v1/capability/auth/start",
                    {
                        "package": package.model_dump(mode="json"),
                        "target_slave": target,
                        "workspace_id": workspace_id,
                        "instance_name": instance_name,
                        "browser_auth": None,
                        "activation_revision": activation_revision,
                        **(fencing or {}),
                    },
                )
                report = CapabilityHealthReport.model_validate(
                    response.get("health_report") or response
                )
            else:
                raise RuntimeError("slave_not_found")
            await repository.record_capability_health(report)
            reports.append(report.model_dump(mode="json"))
        except (RuntimeError, ValueError) as exc:
            reports.append({"target_slave": target, "activation_state": "failed", "error": str(exc)})
    return reports


__all__ = ["execute_activation_targets", "execute_auth_targets"]
