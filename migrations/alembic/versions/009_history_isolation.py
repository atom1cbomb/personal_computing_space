"""Separate append-only capability audit from frequently read projections."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from loom_v2.db.alembic_context import migration_role

revision = "009_history_isolation"
down_revision = "008_resource_access_bindings"
branch_labels = None
depends_on = None


def upgrade():
    if migration_role() != "observer":
        return
    op.add_column("runs", sa.Column("latest_event_at", sa.String(64), nullable=False, server_default=""))
    op.execute("UPDATE runs SET latest_event_at = COALESCE((SELECT max(e->>'created_at') FROM jsonb_array_elements(events) e WHERE jsonb_typeof(e->'created_at') = 'string'), '')")
    op.create_index("idx_runs_state", "runs", ["state"])
    op.create_table("capability_audit_events",
        sa.Column("workspace_id", sa.String(128), primary_key=True),
        sa.Column("package_ref", sa.String(512), primary_key=True),
        sa.Column("sequence", sa.Integer(), primary_key=True),
        sa.Column("report_id", sa.String(256), nullable=True),
        sa.Column("event", JSONB(), nullable=False),
        sa.UniqueConstraint("workspace_id", "package_ref", "report_id", name="uq_capability_audit_report"))
    op.execute("LOCK TABLE workspace_capability_packages IN SHARE ROW EXCLUSIVE MODE")
    op.execute("""INSERT INTO capability_audit_events
        SELECT workspace_id, package_ref, ordinal::integer,
               CASE WHEN e->>'phase' = 'capability_health_report' THEN e->>'report_id' END, e
        FROM workspace_capability_packages CROSS JOIN LATERAL jsonb_array_elements(events) WITH ORDINALITY AS a(e, ordinal)
    """)
    # Keep the legacy column as a frozen pre-upgrade copy. Runtime never rewrites it.


def downgrade():
    if migration_role() != "observer":
        return
    op.execute("LOCK TABLE workspace_capability_packages IN SHARE ROW EXCLUSIVE MODE")
    op.execute("""UPDATE workspace_capability_packages p SET events = COALESCE(
        (SELECT jsonb_agg(event ORDER BY sequence) FROM capability_audit_events e
         WHERE e.workspace_id=p.workspace_id AND e.package_ref=p.package_ref), '[]'::jsonb)
    """)
    op.drop_table("capability_audit_events")
    op.drop_index("idx_runs_state", table_name="runs")
    op.drop_column("runs", "latest_event_at")
