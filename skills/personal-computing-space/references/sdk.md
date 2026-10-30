# SDK Access and Metadata

## Install

The SDK is independent of the skill instructions and the Loom server.
Release archives include one SDK wheel in `assets/wheels/`; the source skill
directory does not contain a checked-in binary.

Check the Python environment that will actually run the agent:

```sh
python -m pip show loom-orchestration
python -c "from loom_orchestration import LoomClient"
```

If the distribution is missing, tell the user: “当前 Python 环境未安装 Loom Python
SDK（loom-orchestration），需要先安装。” Include the exact matching download URL
and a `python -m pip install` command. Install into the selected environment when
covered by the task's authorization; do not treat a checkout on `sys.path` as an
installed SDK. If the distribution exists but import fails, report that error
and repair its installation/dependencies instead of claiming it is ready.

The operator-provided Observer base URL exposes public release downloads:

| Path | Artifact |
| --- | --- |
| `/downloads/personal-computing-space.zip` | Skill, references and matching bundled SDK wheel |
| `/downloads/manifest.json` | `skill` and `sdk` filenames, sizes and SHA-256; SDK version |
| `/downloads/<sdk.filename>` | Standalone Python SDK wheel, using the filename returned by the manifest |

These downloads need no GRIP token. They contain release files only; resource
operations still need the operator's authentication. Use a trusted Observer URL.
Each Observer image builds both artifacts from its own source. After an Observer
upgrade, fetch its manifest again and install the matching SDK when an update is
needed; do not reuse a filename or infer the installed version from this skill.

For a skill copied from source or to download the current standalone wheel, set
`LOOM_OBSERVER_URL` to the operator's Observer URL. This bootstrap uses only the
Python standard library and verifies the downloaded bytes:

```sh
python - <<'PY'
import hashlib, json, os, shlex, sys, urllib.request
from pathlib import Path

base = os.environ["LOOM_OBSERVER_URL"].rstrip("/")
with urllib.request.urlopen(base + "/downloads/manifest.json", timeout=30) as response:
    sdk = json.load(response)["sdk"]
name = sdk["filename"]
assert Path(name).name == name and name.startswith("loom_orchestration-") and name.endswith(".whl")
url = base + "/downloads/" + name
with urllib.request.urlopen(url, timeout=60) as response:
    content = response.read()
assert len(content) == sdk["size"]
assert hashlib.sha256(content).hexdigest() == sdk["sha256"]
wheel = Path(name).resolve()
wheel.write_bytes(content)
print("SDK", sdk["version"], url)
print(shlex.join([sys.executable, "-m", "pip", "install", "--upgrade", str(wheel)]))
PY
```

Run the printed installation command in the chosen Python environment, then
repeat the import check. A 404 means this Observer has no published client
artifact at that address; use its release bundle or ask the deployment operator
to upgrade it, rather than guessing an alternate server route or a PyPI package.

Using Python 3.12+, set `SKILL_DIR` to this installed skill's directory:

```sh
python -m venv .loom-agent
.loom-agent/bin/python -m pip install "$SKILL_DIR"/assets/wheels/loom_orchestration-*.whl
.loom-agent/bin/python -c "from loom_orchestration import LoomClient, ResourceEffect, SubmissionBuilder"
```

Dependencies are Pydantic, httpx, httpcore, websockets, jsonschema and rfc8785. In an offline environment, obtain their
compatible wheels from the operator as well; the skill archive contains only
the Loom SDK wheel. No server modules, Docker SDK, database or model provider
are required in the external agent environment.

If Observer downloads and a release bundle are unavailable, an available trusted
checkout can be installed with
`python -m pip install /path/to/loom_v2/packages/loom-orchestration`.
Do not assume `pip install loom-orchestration` resolves to this project's release.

## Resource Discovery and Invocation

Use GRIP for resource access. Configure the Observer URL, GRIP authority and
workspace token supplied by the operator; credentials belong in request auth.
`LoomClient.resources()` lists the logical Driver, Slaves, capability-package
definitions, activations, registered resources, frozen plans and Runs in one directory. `describe(uri)`
returns offers with complete action and Schema URIs. `invoke(uri, action, input)`
discovers a trusted binding and validates the input/output contracts.
The MCUE contract uses `resources(mcue=...)` and `resource.mcue`;
see [mcue.md](mcue.md) for definitions, attribute updates, and release requirements.

```python
async with LoomClient(observer_url, token=grip_token,
                      grip_authority=grip_authority) as client:
    resources = await client.resources(mcue="<category/subtype>")
    description = await client.describe(selected_target_uri)
    result = await client.invoke(selected_target_uri, selected_action_uri, action_input)
```

The previous `slaves`, `resource`, `resource_activations` and `start_auth`
methods have been removed. Use resource descriptions and actions instead.
Select exact returned URIs; do not infer an action from its suffix. Slave
`run-package` input is `{package, action, arguments, idempotency_key}`. It
returns `{run: <Run URI>}`. Query Run with the built-in `read` action.
SDK 0.10's task helpers use GRIP: `submit` discovers `computing/driver` and invokes
`submit-plan`, then `get-submission`; `submission` looks up the business key;
`run`, `cancel`, `resubmit`, `resolve` and `close` invoke Run actions and accept
the returned Run URI or opaque final ID. `run` requests `include_history=true`.
The SDK `request_id` parameter maps to business `idempotency_key`; the GRIP
message ID is independent. `submit` and `resubmit` raise `PlanRejectedError`
for a readiness rejection, preserving `.code` and `.details["blockers"]`;
raw `invoke` returns that domain result under `rejected`. Omit `request_id` on `cancel` unless checking a known Driver
submission belongs to the Run. Action-specific `run-package`/activation keys
are not Driver submission keys. The matching Observer release removes the old
task REST routes. See [observer-workflow.md](observer-workflow.md) for polling,
immutable Plan reads, repair, attestation and cancellation.
An activated instance's `interaction=task` export instead uses
`{arguments, expected_revision, idempotency_key}` and returns a Run.
`request-reply` exports take business input and return business output directly;
`byte-stream` exports use `open_stream`. Read [service-bindings.md](service-bindings.md)
for trusted routes, scoped credentials, `configure`, and dependency contexts.
Use `activate-package` on a Slave for named service instances, then `auth-start`
when offered by a pending-auth instance. This does not create a second run.

On the activation-credentials server release, `activate-package` takes optional
`credential_profile` alongside `instance_name`, `instance_config`, `target` and
`idempotency_key`. Pass an operator-supplied profile name, not a file path or
secret. Each named instance can select a different profile from the Slave's
configured credential directory. New instances with no selection mount no
credentials; omission/null on an existing instance preserves its binding.
Retries and restarts preserve that selection. Switching profiles requires a
new instance name. The profile is private activation configuration and is not
returned by resource DESCRIBE or copied to MCUE attributes. New publications
reject `body.credential_profile`; never fall back to embedding it in a package.

Inside a program, use `await ctx.ensure_activation(...,
credential_profile="operator-provided-profile")`. The choice participates in
the step's idempotency check. This argument requires SDK 0.6.0 and the matching
Observer, Driver, Slave and runtime release; installing only the wheel does
not upgrade the deployment. Check the deployed `activate-package` input schema
before use. Whether a profile is required is declared by the selected package
and activation contract; do not infer it from a provider or resource name.

Activation returns its resource URI automatically. DESCRIBE includes all declared
business actions in `resource.metadata.actions`: names, descriptions, descriptor
and IoContract refs, business `arguments_schema` / `result_schema` refs, effects,
permissions and availability. Pending, failed or stopped instances retain those
declarations with `available=false`; only currently callable actions appear in
`offers`. Business terminology is `action_exports`, `action_descriptor_ref`,
`action_ref` and descriptor `action.v1`. Old business fields are not aliases.

Package authoring retains `upload_content`, `register_package`, `packages`,
`descriptor` and `read_json`; full orchestration uses the GRIP-backed `submit`.
Package publication returns its complete Observer-assigned URI in `resource`;
use `published["resource"]` and `published["package_digest"]` to form a ResourceRef.
Package list/detail/read responses carry the same field. `ServicePackage.publish`
returns that URI directly. Never reconstruct it from package coordinates.
These expose immutable source and metadata for program construction. Uploaded
descriptors use a content resource id plus a semantic descriptor digest;
preserve both. Resource metadata describes relationships; it grants no runtime
readiness or permission by itself.

## Mapping and Program Rules

For ordinary Python computation, [function-packages.md](function-packages.md)
contains the complete public authoring contract and a generic publication example.

`function + process:json_stdio/1` packages have exactly one export. For a
synthetic lifecycle test, publish separate single-export packages (create,
status, use, cleanup), or use one action with an explicit action input.
Multiple exports belong to a supporting service executor, not json-stdio.
Metadata paths must be declared in real input/output schemas; an empty
`io.v1` contract cannot validate a resource identity or readiness mapping.
The `io.v1` schema subset is `type`, `required`, `properties`, `items`, `enum`,
`const`, `minimum`, `maximum`, `minLength`, `maxLength`; other keywords are
rejected on schema upload. MCUE attribute schemas have their own contract.

- `identity_path` on an effect is relative to the complete create result.
- Readiness `result_path` selects status items; its `identity_path` and condition
  paths are relative to each item. Match identity before checking readiness.
- Mapping values are field paths, `"$resource.identity"`, arrays of mappings,
  or `{"constant": value}`. Array wrapping is explicit:
  `{"InstanceIds": ["$resource.identity"]}`. Supply required fields such as
  filters through explicit constants, not implicit defaults.
- Readiness input paths and cleanup input paths refer to the create result.
  Access configuration paths refer to the matched readiness item.
- Use bounded polling and stable, distinct `step_id` values per resource,
  iteration and user action. Include all invoked package refs and effects
  in the closure's authorization; metadata alone grants nothing.
- `SubmissionBuilder.allowed_effects` must include the exact `permissions`
  strings from every invoked export, including cleanup. Do not substitute the
  export's `effect_class` or guess permission names. Grant only the actions
  authorized by the user.
- Wait between polls with `await ctx.sleep(seconds)` (0–3600 seconds, bounded
  by the Run deadline). The sandbox does not permit `asyncio`/`time` imports.
- Use existing context primitives in an externally authored
  `async orchestrate(ctx: "OrchestrationContext", input_ref: "ResourceRef") -> "ResourceRef"`
  program. The quoted annotations are required by the published typecheck contract.
  No Observer renderer, plan DSL,
  internal API or Python server-module imports are needed.
- Inside the orchestrator, refs are JSON mappings supplied by `ctx` or read from
  input. Do not import the external SDK's Pydantic `ResourceRef` or call
  `model_validate`/`model_dump` there: that class is different from the sandbox's
  `ResourceRef` mapping type. Convert SDK models to JSON only in the external
  authoring client before uploading inputs.
- `emit_node.target_resource_ref` identifies a registered domain resource, not a
  Slave placement address. For ordinary functions omit it and let runtime select
  eligible Slaves; verify actual attempt targets for multi-node requirements.
  To use a particular existing instance, pass its resolved `activation_ref`, which
  carries the server-observed Slave and instance binding.
- `emit_node(..., input_refs=[ref])` passes that content's JSON value unchanged.
  Zero or multiple refs produce `{"inputs": [value_for_each_ref]}` in the same
  order. The node's input schema and source must accept that exact shape;
  passing several result refs does not automatically merge their fields or
  select a different action. Use a separately authored merge function and its
  descriptor, or a single function whose declared contract supports both forms.
  Validate representative node inputs locally before spending a Run budget.
- Return a completed node's `ctx.result(handle)` ref. `materialize_json`
  creates inputs; returning an unrelated materialized ref fails lineage validation.
  Raise `OrchestrationFailure({"code": "..."})` for an explicit program failure;
  arbitrary exception classes such as `RuntimeError` are not sandbox builtins.
- Authentication is `awaiting_auth` on the same Run. Never put credentials
  in source, metadata, input content, or events.

Publication templates may contain unresolved refs. Resolve referenced actions
and access packages through public discovery and use exact returned digests
before uploading the descriptor. Publish access dependencies first; related
actions in the same package can refer to each other's descriptor digests.
Observer rejects missing dependencies, missing required inputs, invalid schema
paths and incompatible mapping types rather than choosing a relationship.

## Durable Cleanup

Inside an orchestration program, call `ctx.register_resource` with `mcue_ref`,
`identity`, optional public `attributes` and optional `managed_by_ref`, then `defer_cleanup`
with a distinct stable step id. Registration reserves one node from `max_nodes`;
budget for both normal nodes and cleanup. `resource_budget.cleanup_timeout_seconds`
is a separate terminal cleanup deadline (default 60 seconds, allowed 1–300).
Set `resource_budget.execution_timeout_seconds` on `SubmissionBuilder` to cover
provisioning, readiness polling and interactive login (for example, 1800 seconds
for an interactive resource test). This is the server-side Run budget. Agent
wall-clock time and client polling time are separate: the SDK's default Run
polling follows the server terminal state, and an explicit polling timeout is
only a local watchdog. Authentication waiting uses this original execution
deadline and does not clean up. Once cleanup starts, the submission's
`deadline_at` reflects the separate cleanup deadline, not the original budget.

The Observer persists each exact cleanup action and its materialized input.
For `defer_cleanup`, use `$resource.identity`, arrays, or `{"constant": value}`;
resolve other metadata paths from the create result before passing constants.
Driver executes registered actions in registration order after success,
failure, cancellation or timeout, without rerunning the orchestration program.
Poll through `cleaning_up` to a final state and inspect `outcome.cleanup`.
Completed cleanup nodes are reused after restart. Unconfirmed main side effects
block automatic cleanup and produce `execution_recovery_required`; failed cleanup
retains the resource refs. Neither means the resource was released.

Access configuration must satisfy the descriptor's `instance_config_schema_ref`.
Only non-sensitive connection fields belong there. `wait_activation` accepts
`ready`; `authenticated` additionally requires confirmed authentication status.
These guarantees require the matching server release, not just an updated wheel.
Configuration and public attributes are separate: the MCUE schema determines
which descriptive fields may be published, and a description update does not
change the activation's actual connection settings.

For an existing named instance, SDK 0.8.1 provides the read-only
`await client.activation_ref(activation_uri)`. It preserves discovered package,
host, name and revision for `wait_activation` and `emit_node`; it does not start,
replace or authenticate the instance. See [existing-activations.md](existing-activations.md).

## Publish a service artifact directory

`ServicePackage(directory).publish(client, image_ref=immutable_image,
dependencies={coordinate: published_grip_uri})` uploads schemas, validators,
IoContracts and descriptors in dependency order, then returns the published GRIP
package URI. Access packages must be published first. The helper uses actual
Observer-returned references, never placeholder digests. Publication does not
activate or invoke the package. Outside a program, domain resource registration
uses the workspace's GRIP `register-resource` action. Read its offered input
schema for the deployed fields. Registration requires `resource_id` and
`mcue_ref`, accepts initial `attributes`, and has optional `managed_by`.
SDK 0.11 and the matching Observer remove the domain resource's `package_ref`/`package`;
select the access package when activating an instance bound to the resource.
`metadata.activations` is computed from ready bindings on each read, not stored
on the domain resource. Update existing attributes with `update-attributes` and an
observed `expected_attributes_revision`.


Resource names are GRIP URIs throughout, including uploaded content and package internals. Use `upload_content(..., name="cloud/schemas/instances-output")` for a readable immutable content name; use `read_json(ref)` to read it through GRIP. Content names end in `/sha256/<digest>` but always include a readable purpose before that suffix. Use the package URI returned by Observer in `resource`; the immutable digest is an explicit contract field. Pass the returned URI unchanged: do not wrap it under another `resources/` path. `ServicePackage.publish` relocates template references to the client's configured authority and workspace.

## VM file streams (SDK 0.12)

For an activation declaring `files.upload` and `files.download` with byte-stream
interaction, use `VmFileClient.discover(client, activation_uri)`, then
`upload(local_path, remote_path)` or `download(remote_path, local_path)`. The
`loom-vm-files` CLI provides the same operations. Overwrite is explicit, and the
helper verifies byte length and SHA-256 before reporting success or publishing
a downloaded file. Read the selected package HOW_TO_USE for framing, paths,
permissions, authentication and uncertain-upload handling. Existing packages
without these declarations require a matching package upgrade; SDK installation
alone does not add file actions. File bytes do not go into Run inputs or content
uploads.
