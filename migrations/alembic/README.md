# Loom role-local Alembic migrations

The first public release starts from one squashed `001_release_baseline`
revision. It creates only the schema owned by the selected role: Observer or
Slave. The runtime supplies the role and an existing SQLAlchemy connection
through `loom_v2.db.migrations`.

Application startup and `scripts/migrate.sh` both call this adapter. Future
schema changes must add a new Alembic revision; the release baseline is
immutable.

## 008 resource access bindings

`008_resource_access_bindings` removes `package_ref` from live Observer
`workspace_resources.resource` JSON. The resource URI, attributes and their
revision, state, and manager remain unchanged. Activation package references,
target bindings, authentication configuration, published packages, and historical
Runs are untouched. The Slave revision advances without a data change.

Before removal, the same transaction copies every registration containing the
field (including null values) into `workspace_resource_package_archive`. This
migration-only table stores the workspace, resource URI, exact old JSON,
`attributes_revision`, and `archived_at`. Later resource updates never change the
archive. The Alembic version prevents repeat migration; a failure rolls back both
the archive and the live-row update.

Upgrade Observer, Driver, the SDK (0.11), skill, and resource-registering callers
together. Before upgrading, finish or cancel active orchestration Runs that use
resource registration, then stop old Observer/Driver writers and back up the
database and content store. Even an old registration with no explicit package
argument recorded `package_ref: null` in its replay evidence. Stored source and
frozen plans are immutable; publish a new program version without the removed
argument for subsequent submissions. Historical Runs remain readable; their old
registration calls and replay evidence are not adapted to the new contract.

Run the existing `scripts/migrate.sh observer` entry point with the deployment's
database configuration, or let Observer startup apply the migration. Databases
that predate MCUE still need the explicit MCUE backfill before service startup;
the package-removal migration does not infer resource types. SQLite is used for
fresh test databases; production migrations require PostgreSQL.

Verify that DESCRIBE omits the domain resource's `metadata.package`, that ready
`activations` still point to the same target URI, and that their business offers
are unchanged. The registry has no persisted activations array to update.

To inspect an old registration, query the archive by both workspace and resource:

```sql
SELECT resource, attributes_revision, archived_at
FROM workspace_resource_package_archive
WHERE workspace_id = :workspace_id AND resource_id = :resource_id;
```

Downgrade requires a coordinated application/database backup restore. The
migration deliberately does not overwrite current resource rows with old
snapshots, which would discard edits made after upgrade.
