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
- `resource.mcue` is a lowercase `category/subtype`, such as `computing/vm`.
  This replaces resource-level `kind` across all resource descriptions.
- `metadata.mcue_ref` names the immutable, versioned MCUE definition. Use the
  Observer-returned GRIP URI and its discovered `read` action.
- `metadata.attributes` holds public values validated by that definition's
  `attributes_schema`; `metadata.attributes_revision` protects their updates.
- `package`, `managed_by`, `state`, and computed `activations` remain system
  metadata. They are not entries in the user-editable attributes object.

The SDK filter is `client.resources(mcue="computing/vm")`; the
`list-resources` action likewise takes `mcue`. The replacement has no old field
alias. It does not rename GRIP envelope `kind` values such as `reply` and `error`,
executor `execution.kind`, `capability_kinds`, or Kubernetes `kind`.

Platform MCUE labels are `loom/workspace`, `computing/slave`,
`capability/package`, `capability/activation`, `execution/run`, `grip/action`,
`grip/schema`, `data/content`, and `grip/mcue-type`. Each description references
its definition. A definition's body contains `mcue`, `version`, `description`,
and `attributes_schema`; its resource type is itself `grip/mcue-type`.
Platform system metadata still comes from its authoritative records; generic
attribute updates initially apply to registered domain resources.

Type labels describe resources, not authorization or available actions.
Use actual offers and ready activation bindings to decide how to execute.
Do not derive types from package names, URI paths, IP addresses, or capability
kinds. MCUE also does not replace a content resource's byte-format `media_type`.

## Public attribute policy

Read the selected MCUE definition to determine the allowed attributes and
their meaning. The initial `computing/vm/v1` definition allows `display_name`,
`description`, `labels`, `cpu_cores`, `memory_bytes`, `os`, and `disks`.
It uses `display_name` and `labels`, without `name` or `tags` aliases.
Unknown values can be omitted; do not invent hardware measurements.

`computing/volcano/v1` describes a Volcano Dashboard. Its required public fields
are `dashboard_url` (HTTPS origin, including the port) and `kubernetes_ref`
(associated Kubernetes GRIP URI); display_name, description and labels are
optional. See [volcano.md](volcano.md) for discovery and workload/GPU queries.

`computing/grafana/v1` describes a Grafana monitoring endpoint. Required public
fields are `dashboard_url` and `kubernetes_ref`; `volcano_ref` is an optional
explicit link to the related Volcano resource. See [grafana.md](grafana.md) for
metric discovery, credentials and actual GPU utilization.

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
it does not create a Driver Run, require the VM to be online, or change an
activation revision. It cannot update system metadata or execution configuration.
