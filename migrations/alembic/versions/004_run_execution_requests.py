"""Persist Observer execution queue items."""

from alembic import op

from loom_v2.db.alembic_context import migration_role


revision = "004_run_execution_requests"
down_revision = "003_workspace_resources"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql" or migration_role() != "observer":
        return
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS run_execution_requests (
            workspace_id VARCHAR(128) NOT NULL,
            request_id VARCHAR(256) NOT NULL,
            request_payload JSONB NOT NULL,
            run_id VARCHAR(128) NOT NULL,
            closure_version VARCHAR(128) NOT NULL,
            closure_contract JSONB NOT NULL,
            snapshot_digest VARCHAR(128) NOT NULL,
            source VARCHAR(32) NOT NULL DEFAULT 'external',
            conversation_ref VARCHAR(256),
            state VARCHAR(32) NOT NULL DEFAULT 'queued',
            driver_id VARCHAR(128),
            driver_instance_id VARCHAR(128),
            driver_epoch BIGINT,
            execution_id VARCHAR(128),
            execution_epoch INTEGER,
            outcome JSONB,
            started_at TIMESTAMPTZ,
            deadline_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL,
            PRIMARY KEY (workspace_id, request_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_run_execution_requests_queue "
        "ON run_execution_requests (workspace_id, state, created_at)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_run_execution_requests_run "
        "ON run_execution_requests (workspace_id, run_id)"
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_run_execution_request_active "
        "ON run_execution_requests (workspace_id, run_id) "
        "WHERE state IN ('queued', 'claimed', 'recovering')"
    )
