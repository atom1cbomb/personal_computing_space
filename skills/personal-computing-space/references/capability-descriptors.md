# Capability descriptors

The project skill does not carry provider or resource behavior. A capability
package publishes that behavior in its immutable manifest, action descriptors,
schemas, contracts and (when needed) public source/documentation references.
Use this procedure whenever a task mentions a concrete service or resource.

## Resolve a capability

1. Call the public inventory/list action and enumerate the relevant resource and
   activation records. Keep each returned URI as an opaque value. For each
   record retain the system metadata (`package`, `managed_by`, `state`,
   `activations`, revision) and public MCUE attributes. Inventory is an index,
   not a behavior contract.
   Retain `metadata.dependencies` and read each entry's declared meaning; an
   absent declaration is unknown. DESCRIBE supplies the same runtime references.
2. Resolve the resource's returned `metadata.mcue_ref` through its exact public
   MCUE read action. Use that versioned definition to interpret public
   attributes; do not copy an attribute list from this skill. If the selected
   package publishes a separate relationship or identity schema, resolve that
   schema too.
3. DESCRIBE the resource and the selected activation. Select an offered action
   by its exact returned URI. Read its `interaction`, argument/result schemas,
   permissions, dependency grants and descriptor references. A ready state or
   `available=true` does not describe the business behavior by itself.
4. If a descriptor is inline, validate its digest and use that payload. If it
   is referenced, resolve it with
   `GET /api/v1/action-descriptors?resource_id=...&version_or_digest=...` and the
   exact resource ID plus version or digest returned by Observer. Resolve referenced
   input/output schemas, IO contracts, validators, packages and actions before
   dispatch. Never fetch a private runtime endpoint or reconstruct a URI.
5. For a resource-producing action, use the descriptor's `resource_effects`:
   its identity path, MCUE type, input mappings, readiness condition,
   `access_requirements`, authentication requirement and cleanup action are the
   only source for the workflow. Match identities exactly before evaluating
   readiness. Record cleanup before starting a side effect.
   Read `attributes_mapping` and the target MCUE dependency declarations before
   creation, and fill the required attributes at resource registration.
6. For an ordinary action, use the declared interaction to decide whether to
   pass business input, follow a Run, or open a stream. Use the declared result
   schema and validator to interpret the result. Keep the original result
   reference and digest in the evidence.

## Match relationships

`managed_by` is system metadata that can show which package or management
record owns a resource. It is useful provenance evidence and can distinguish an
externally registered resource from one produced by a package, but it is not a
universal provider label and its meaning is deployment-defined. Use it together
with the package descriptor and declared identity/relationship fields.

Do not infer relationships from an IP address, display name, package name,
activation target, URI spelling or a successful call. Distinct resource URIs
remain distinct unless the deployed MCUE or capability descriptor explicitly
declares their relationship. When the metadata or descriptor is absent,
conflicting or stale, stop with a metadata blocker and report the evidence.

## Package documentation and source

Read package documentation or source only when the package exposes an immutable
public reference for the selected version. It can explain provider semantics,
but it cannot override deployed action descriptors or schemas. Do not use a
source checkout, historical README or another package's examples to fill in a
missing contract.
