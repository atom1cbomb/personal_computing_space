"""One-time, explicit backfill of resource types before starting an upgraded Observer."""
from __future__ import annotations

from typing import Any

from pydantic import Field
from sqlalchemy import select, text

from loom_orchestration.grip import Model, URI
from loom_v2.contracts.types import ManagedResource
from loom_v2.db.models import WorkspaceResourceRow


class BackfillEntry(Model):
    mcue_ref: URI
    attributes: dict[str, Any] = Field(default_factory=dict)


async def backfill_resource_mcue(repository, workspace_id: str, mapping: dict[str, Any]) -> int:
    """Validate the complete explicit mapping, then commit all rows atomically.

    Existing typed resources are never rewritten. This is a deployment migration,
    not a second public registration or attribute editing path.
    """
    entries = {target: BackfillEntry.model_validate(value) for target, value in mapping.items()}
    for value in entries.values():
        definition = await repository.get_mcue_definition(workspace_id, value.mcue_ref)
        definition.validate_attributes(value.attributes)
    changed = 0
    async with repository._resource_transaction(workspace_id, "mcue-backfill") as session:
        if session is None:
            raise ValueError("mcue_backfill_database_required")
        if session.bind.dialect.name == "postgresql":
            for target in sorted(entries):
                await session.execute(
                    text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                    {"key": f"loom:resource:{workspace_id}:{target}"},
                )
        rows = (await session.scalars(
            select(WorkspaceResourceRow).where(WorkspaceResourceRow.workspace_id == workspace_id).with_for_update(),
        )).all()
        indexed = {row.resource_id: row for row in rows}
        if set(entries) - set(indexed):
            raise ValueError("mcue_backfill_resource_not_found")
        if any("mcue_ref" not in row.resource and row.resource_id not in entries for row in rows):
            raise ValueError("mcue_backfill_mapping_incomplete")
        for target, value in entries.items():
            row = indexed[target]
            if "mcue_ref" in row.resource:
                if row.resource["mcue_ref"] != value.mcue_ref:
                    raise ValueError("mcue_backfill_type_conflict")
                continue
            resource = ManagedResource.model_validate({
                **row.resource, **value.model_dump(), "attributes_revision": 1,
            })
            row.resource = resource.model_dump(mode="json", exclude={"attributes_revision"})
            row.attributes_revision = 1
            changed += 1
    return changed
