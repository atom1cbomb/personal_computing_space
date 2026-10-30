"""Archive resource registration packages; access belongs to activations."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from loom_v2.db.alembic_context import migration_role

revision = "008_resource_access_bindings"
down_revision = "007_activation_credentials"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql" or migration_role() != "observer":
        return
    # The migration transaction preserves the exact old registration before
    # removing its redundant package reference. Runtime never writes this archive.
    op.create_table(
        "workspace_resource_package_archive",
        sa.Column("workspace_id", sa.String(128), primary_key=True),
        sa.Column("resource_id", sa.String(512), primary_key=True),
        sa.Column("resource", JSONB(), nullable=False),
        sa.Column("attributes_revision", sa.Integer(), nullable=False),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.execute("LOCK TABLE workspace_resources IN ACCESS EXCLUSIVE MODE")
    op.execute("""
        INSERT INTO workspace_resource_package_archive
            (workspace_id, resource_id, resource, attributes_revision)
        SELECT workspace_id, resource_id, resource, attributes_revision
        FROM workspace_resources WHERE resource ? 'package_ref'
    """)
    op.execute("""
        UPDATE workspace_resources SET resource = resource - 'package_ref'
        WHERE resource ? 'package_ref'
    """)


def downgrade() -> None:
    raise RuntimeError("resource_access_bindings_downgrade_requires_backup_restore")
