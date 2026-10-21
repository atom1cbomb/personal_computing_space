"""Move the credential selection into existing activation records.

Package manifests are immutable evidence and are deliberately left untouched.
"""
from alembic import op
import sqlalchemy as sa

from loom_v2.db.alembic_context import migration_role

revision = "007_activation_credentials"
down_revision = "006_mcue_attributes"
branch_labels = None
depends_on = None


def _backfill(activations, profiles):
    return [
        item if "credential_profile" in item else {
            **item, "credential_profile": profiles.get(item["package_digest"]),
        }
        for item in activations
    ]


def upgrade() -> None:
    connection = op.get_bind()
    if connection.dialect.name != "postgresql":
        return
    if migration_role() == "observer":
        packages = sa.table("workspace_capability_packages",
            sa.column("workspace_id", sa.String), sa.column("package_ref", sa.String),
            sa.column("package", sa.JSON), sa.column("capability_activations", sa.JSON))
        rows = connection.execute(sa.select(packages)).mappings().all()
        profiles = {
            row["package"]["package_digest"]: (row["package"].get("body") or {}).get("credential_profile")
            for row in rows
        }
        for row in rows:
            connection.execute(packages.update().where(
                packages.c.workspace_id == row["workspace_id"], packages.c.package_ref == row["package_ref"],
            ).values(capability_activations=_backfill(row["capability_activations"] or [], profiles)))
        runs = sa.table("runs", sa.column("run_id", sa.String),
            sa.column("capability_packages", sa.JSON), sa.column("capability_activations", sa.JSON))
        for row in connection.execute(sa.select(runs)).mappings().all():
            run_profiles = {**profiles, **{
                package["package_digest"]: (package.get("body") or {}).get("credential_profile")
                for package in (row["capability_packages"] or [])
            }}
            connection.execute(runs.update().where(runs.c.run_id == row["run_id"]).values(
                capability_activations=_backfill(row["capability_activations"] or [], run_profiles)))
    else:
        replicas = sa.table("slave_replica", sa.column("slave_id", sa.String),
            sa.column("capability_activations", sa.JSON))
        for row in connection.execute(sa.select(replicas)).mappings().all():
            activations = [
                item if "credential_profile" in item else {
                    **item,
                    "credential_profile": ((item.get("package_payload") or {}).get("body") or {}).get("credential_profile"),
                }
                for item in (row["capability_activations"] or [])
            ]
            connection.execute(replicas.update().where(replicas.c.slave_id == row["slave_id"]).values(
                capability_activations=activations))


def downgrade() -> None:
    raise RuntimeError("activation_credentials_downgrade_requires_backup_restore")
