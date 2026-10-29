# MCUE Resource Descriptions

## Implementation status

MCUE (Multipurpose Computing Utility Extension) is implemented in SDK 0.5 and
the matching Observer release. Existing deployments require the database and
explicit resource-type migration before switching releases. Updating the skill
alone does not upgrade the server. Inspect DESCRIBE offers and their input
schemas; if a deployment lacks MCUE support, report the required upgrade instead
of rewriting a registration or calling an internal API.

## Resource identity and type

- `resource.uri` is the stable identity; changing attributes does not rename it.
- `resource.mcue` is a lowercase `category/subtype` selected by the published
  MCUE definition. It replaces resource-level `kind` across resource
  descriptions.
- `metadata.mcue_ref` names the immutable, versioned MCUE definition. Use the
  Observer-returned GRIP URI and its discovered `read` action.
- `metadata.attributes` holds public values validated by that definition's
  `attributes_schema`; `metadata.attributes_revision` protects their updates.
- `package`, `managed_by`, `state`, and computed `activations` remain system
  metadata. They are not entries in the user-editable attributes object.

The SDK filter is `client.resources(mcue="<category/subtype>")`; the
`list-resources` action likewise takes `mcue`. Replace the placeholder with the
type declared by the selected MCUE definition. The replacement has no old
field alias. It does not rename GRIP envelope `kind` values such as `reply` and
`error`, executor `execution.kind`, capability kinds, or foreign protocol
`kind` fields.

Platform MCUE labels include platform records, capability packages and
activations, executions, actions, schemas, content and MCUE definitions. The
deployed directory is authoritative for the complete set. Each description
references its definition. A definition's body contains `mcue`, `version`,
`description`, and `attributes_schema`; its resource type is itself an MCUE
definition.
Platform system metadata still comes from its authoritative records; generic
attribute updates initially apply to registered domain resources.

Type labels describe resources, not authorization or available actions.
Use actual offers and ready activation bindings to decide how to execute.
Do not derive types from package names, URI paths, IP addresses, or capability
kinds. MCUE also does not replace a content resource's byte-format `media_type`.

## Public attribute policy

Read the selected MCUE definition to determine the allowed attributes and
their meaning. Do not copy a field list from this skill, invent hardware or
service measurements, or assume aliases such as `name`/`tags`. Unknown values
can be omitted. A capability package may publish additional schemas and
relationship fields; those declarations govern the package's behavior, while
the MCUE definition governs public resource attributes.

MCUE decides whether host, user, port, or runtime information may be public.
There is no platform-wide blacklist for those field names. A definition that
declares such fields permits their explicit descriptive values; a closed schema
that omits them rejects them. Credentials normally do not belong in public
attributes; use authentication/runtime credential channels for actual secrets.
Never automatically copy `instance_config` or credential files into attributes.
Even where names overlap, changing a public attribute does not change the
actual connection configuration.

## Publishing, registration, and updates

When the deployment supports MCUE, discover the following actions:

- Workspace `publish-mcue-type`: publish `mcue`, `version`, `description`, and
  `attributes_schema`; retain its authoritative definition URI. Definitions
  cannot be overwritten with different content at the same type/version.
- Workspace `register-resource`: provide `resource_id`, `mcue_ref`, and initial
  `attributes`; `package` and `managed_by` are optional. Resource-producing
  effects supply an explicit `mcue_ref`. Re-registration is not an attribute
  editing path.
- Resource `update-attributes`: send `expected_attributes_revision` and `patch`.
  Objects merge recursively, omitted keys remain, arrays replace, and null
  deletes a key. The complete merged object must pass the MCUE schema.

For an update, read the resource and definition first, invoke the exact offered
action with the observed revision, then verify the resulting description.
On conflict, read again and reconsider the intended patch; do not blindly
overwrite another update. An optional new `mcue_ref` switches versions of the
same MCUE with the patch in one transaction.

An attribute update returns the resource URI and new attribute revision directly;
it does not create a Driver Run or change an activation revision. It cannot
update system metadata or execution configuration.

## Dependency declarations

In an MCUE `attributes_schema`, mark an explicit string or array-of-string
property with `"x-loom-dependency": true` and a nonempty `description` explaining
the relationship. Use `required` and (for nonempty collections) `minItems` to
require references. Names and meanings belong to the selected type; the platform
does not prescribe a common host, network or storage field. Explicit nested
`properties`/`items` paths are supported. Dependency markers inside `$defs`,
conditional branches or pattern properties are rejected rather than ignored.

Registered values appear in `metadata.attributes`. DESCRIBE and the workspace
`list-resources` action also return `metadata.dependencies` entries with `path`,
`resource` (an exact GRIP URI), and `description`. No declaration gives `null`;
a declared optional collection with no references gives `[]`. Inventory retains
these fields even for unavailable Slaves. Follow the referenced resource's
current description to investigate availability or deletion impact; no log
inspection or inference from IP addresses is necessary for a declared relation.

Package instance configuration uses the same annotation via
`instance_config_schema_ref`. Upload these schemas with
`application/vnd.loom.instance-config-schema+json`; `ServicePackage.publish`
selects that type from descriptor references automatically. Only annotated
references are projected publicly from activation config. Keep credentials in
their existing authentication channels. `how-to-use.dependency_fields` explains
the schema and filling requirements without copying runtime relationship values.

Creation descriptors use `resource_effects[].attributes_mapping` to fill each
target top-level attribute, for example:

```json
{
  "host_resource": {"source": "input", "path": "host"},
  "display_name": {"source": "output", "path": "name"}
}
```

Sources are `input`, `output`, `instance_config` with explicit object paths, or
`constant` with a `value`. The example applies only to a type declaring these
fields. Publication and deployment checks require all mandatory creation
attributes to have mappings, check source paths and basic types, and validate
constants. Preflight also validates known config values. Final resource
registration validates provider results after creation. The SDK helper fills
attributes; it does not execute creation or register anything by itself.

For operator-managed Slaves, deployment config accepts
`resource_metadata = {mcue_ref, attributes}` and passes it on registration.
The built-in `computing/slave/v2` declares a required `host_resource`; other
versions may declare different relationships. Validation runs before issuing a
lease, and stored metadata remains available after heartbeat expiry. Publish
new immutable type/package versions and upgrade Observer/SDK together; skill
changes alone do not change an existing deployment.
