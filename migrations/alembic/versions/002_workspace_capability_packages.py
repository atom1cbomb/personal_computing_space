"""Persist capability packages uploaded outside a Run."""

from alembic import op

from loom_v2.db.alembic_context import migration_role


revision = "002_workspace_packages"
down_revision = "001_release_baseline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    if migration_role() != "observer":
        return
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS workspace_capability_packages (
            workspace_id VARCHAR(128) NOT NULL,
            package_ref VARCHAR(512) NOT NULL,
            package JSONB NOT NULL,
            capability_activations JSONB NOT NULL DEFAULT '[]'::jsonb,
            events JSONB NOT NULL DEFAULT '[]'::jsonb,
            PRIMARY KEY (workspace_id, package_ref)
        )
        """
    )
