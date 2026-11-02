"""Multiple Driver owners and append-only Run history."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from loom_v2.db.alembic_context import migration_role

revision = "010_driver_pool"
down_revision = "009_history_isolation"
branch_labels = None
depends_on = None


def upgrade():
    if migration_role() != "observer":
        return
    op.drop_index("uq_runtime_agents_active_driver", table_name="runtime_agents")
    op.create_table("run_events",
        sa.Column("run_id", sa.String(128), primary_key=True),
        sa.Column("sequence", sa.Integer(), primary_key=True),
        sa.Column("event", JSONB(), nullable=False))
    op.execute("LOCK TABLE runs IN SHARE ROW EXCLUSIVE MODE")
    op.execute("""INSERT INTO run_events SELECT run_id, ordinal::integer, e
        FROM runs CROSS JOIN LATERAL jsonb_array_elements(events) WITH ORDINALITY AS a(e, ordinal)""")


def downgrade():
    if migration_role() != "observer":
        return
    op.execute("LOCK TABLE runs IN SHARE ROW EXCLUSIVE MODE")
    op.execute("""UPDATE runs r SET events = COALESCE(
        (SELECT jsonb_agg(event ORDER BY sequence) FROM run_events e WHERE e.run_id=r.run_id), '[]'::jsonb)""")
    op.drop_table("run_events")
    # Stop Driver processes before downgrading; the old protocol requires a new registration.
    op.execute("UPDATE runtime_agents SET lease_state='expired' WHERE role='driver'")
    op.create_index("uq_runtime_agents_active_driver", "runtime_agents", ["workspace_id"], unique=True,
        postgresql_where=sa.text("role = 'driver' AND lease_state = 'active'"))
