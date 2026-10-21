"""Versioned MCUE definitions and resource attribute revisions."""
from alembic import op
import sqlalchemy as sa

from loom_v2.db.alembic_context import migration_role

revision = "006_mcue_attributes"
down_revision = "005_slave_action_name"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql" or migration_role() != "observer":
        return
    op.add_column("workspace_resources", sa.Column("attributes_revision", sa.Integer(), nullable=False, server_default="1"))
    op.create_table(
        "mcue_definitions",
        sa.Column("workspace_id", sa.String(128), primary_key=True),
        sa.Column("mcue", sa.String(256), primary_key=True),
        sa.Column("version", sa.String(64), primary_key=True),
        sa.Column("content_ref", sa.JSON(), nullable=False),
    )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql" or migration_role() != "observer":
        return
    op.drop_table("mcue_definitions")
    op.drop_column("workspace_resources", "attributes_revision")
