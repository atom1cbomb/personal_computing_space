"""Name the Slave attempt business action consistently with its wire contract."""

from alembic import op
import sqlalchemy as sa

from loom_v2.db.alembic_context import migration_role


revision = "005_slave_action_name"
down_revision = "004_run_execution_requests"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql" or migration_role() != "slave":
        return
    op.alter_column("slave_attempts", "operation", new_column_name="action", existing_type=sa.String(128))


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql" or migration_role() != "slave":
        return
    op.alter_column("slave_attempts", "action", new_column_name="operation", existing_type=sa.String(128))
