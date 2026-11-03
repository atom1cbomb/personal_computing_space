"""Index Run-owned capabilities separately from execution history."""
from urllib.parse import quote

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from loom_v2.db.alembic_context import migration_role

revision = "011_run_capability_scope"
down_revision = "010_driver_pool"
branch_labels = None
depends_on = None


def package_ref(package):
    parts = ("workspaces", package.get("workspace_id") or "workspace-default", "packages",
             package["package_id"], package["package_version"])
    return "grip://" + (package.get("resource_authority") or "loom.local") + "/" + "/".join(quote(p, safe="") for p in parts)


def upgrade():
    if migration_role() != "observer":
        return
    state = op.create_table("run_capability_states",
        sa.Column("run_id", sa.String(128), primary_key=True),
        sa.Column("workspace_id", sa.String(128), nullable=False),
        sa.Column("capability_packages", JSONB(), nullable=False),
        sa.Column("capability_activations", JSONB(), nullable=False))
    op.execute("LOCK TABLE runs, workspace_capability_packages IN SHARE ROW EXCLUSIVE MODE")
    connection = op.get_bind()
    workspace = sa.table("workspace_capability_packages",
        sa.column("workspace_id", sa.String()), sa.column("package_ref", sa.String()),
        sa.column("package", JSONB()), sa.column("capability_activations", JSONB()), sa.column("events", JSONB()))
    # This one-time backfill reads projection columns only. Historical Run
    # snapshots and their event sequence remain untouched.
    rows = connection.execute(sa.text("SELECT run_id, closure_contract->>'workspace_id' AS workspace_id, "
        "capability_packages, capability_activations FROM runs")).mappings()
    for row in rows:
        owner = row["workspace_id"] or "workspace-default"
        private = [p for p in row["capability_packages"] or [] if p.get("scope", "run_bound") == "run_bound"]
        private_refs = {(package_ref(p), p["package_digest"]) for p in private}
        connection.execute(state.insert().values(run_id=row["run_id"], workspace_id=owner,
            capability_packages=private, capability_activations=[a for a in row["capability_activations"] or []
                if (a["package_version_ref"], a["package_digest"]) in private_refs]))
        for package in row["capability_packages"] or []:
            if package.get("scope") != "workspace_reusable":
                continue
            published = {**package, "workspace_id": package.get("workspace_id") or owner}
            ref = package_ref(published)
            predicate = sa.and_(workspace.c.workspace_id == published["workspace_id"], workspace.c.package_ref == ref)
            existing = connection.execute(sa.select(workspace).where(predicate)).mappings().first()
            activations = [a for a in row["capability_activations"] or []
                           if a["package_version_ref"] == ref and a["package_digest"] == package["package_digest"]]
            if existing is None:
                connection.execute(workspace.insert().values(workspace_id=published["workspace_id"],
                    package_ref=ref, package=published, capability_activations=activations, events=[]))
            else:
                if existing["package"]["package_digest"] != published["package_digest"]:
                    raise RuntimeError("capability_package_identity_conflict:" + ref)
                current = list(existing["capability_activations"] or [])
                keys = {(a["target_slave"], a.get("instance_name", "default")) for a in current}
                missing = [a for a in activations if (a["target_slave"], a.get("instance_name", "default")) not in keys]
                if missing:
                    connection.execute(workspace.update().where(predicate).values(capability_activations=current + missing))


def downgrade():
    if migration_role() == "observer":
        op.drop_table("run_capability_states")
