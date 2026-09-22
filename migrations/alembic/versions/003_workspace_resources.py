"""Persist workspace resource handles."""

from alembic import op

from loom_v2.db.alembic_context import migration_role


revision = "003_workspace_resources"
down_revision = "002_workspace_packages"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    if migration_role() != "observer":
        return
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS workspace_resources (
            workspace_id VARCHAR(128) NOT NULL,
            resource_id VARCHAR(512) NOT NULL,
            resource JSONB NOT NULL,
            PRIMARY KEY (workspace_id, resource_id)
        )
        """
    )
