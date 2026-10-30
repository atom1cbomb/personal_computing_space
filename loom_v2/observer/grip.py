"""Workspace resource directory and GRIP application actions.

The directory projects authoritative contracts and deployment bindings.
Task actions submit durable Runs; service actions use their declared bindings.
"""
from __future__ import annotations

import json
import base64
import hashlib
import hmac
import logging
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Any, Literal
from urllib.parse import unquote, urlsplit

from fastapi import Request as HttpRequest
from fastapi.responses import JSONResponse
from jsonschema import Draft202012Validator
from pydantic import Field, ValidationError

from loom_orchestration.grip import (
    URI, MCUE, Model, Names, Request, Description, GripError, REQUEST_ID, decode,
)
from loom_orchestration.contracts import CredentialProfile
from loom_orchestration.mcue import McueDefinition
from loom_orchestration.resource_dependencies import dependency_fields, resource_dependencies
from loom_v2.observer.resource_metadata import activation_config_schema, validate_activation_config, validate_resource_dependencies
from loom_orchestration.naming import content_digest, resource_label
from loom_v2.contracts.errors import DomainError
from loom_v2.contracts.types import CapabilityPackageVersion, ClosureContract, ResourceRef, TaskClosure
from loom_v2.observer.repository import RunSummary
from loom_v2.observer.activation import execute_activation_targets, execute_auth_targets
from loom_v2.observer.authentication import authentication_view
from loom_v2.digest import digest_json
from loom_v2.service_access import interaction
from . import native_tasks
from .native_tasks import NativeTasks, PlanReadOutput, RunReadInput, RunReadOutput

log = logging.getLogger(__name__)


class Empty(Model):
    pass


class ListInput(Model):
    mcue: MCUE | None = None


class RegisterInput(Model):
    resource_id: URI
    mcue_ref: URI
    attributes: dict[str, Any] = Field(default_factory=dict)
    package: URI | None = None
    managed_by: URI | None = None
    state: str = "unknown"


class UpdateAttributesInput(Model):
    expected_attributes_revision: int = Field(ge=1)
    patch: dict[str, Any]
    mcue_ref: URI | None = None


class ResourceOutput(Model):
    resource: URI


class AttributesOutput(ResourceOutput):
    attributes_revision: int = Field(ge=1)


class HowToUseAttribute(Model):
    name: str
    type: str | list[str] | None = None
    required: bool = False
    description: str = ""


class HowToUseAction(Model):
    action: URI
    mode: Literal["request-reply", "byte-stream"]
    input_schema: URI
    output_schema: URI
    available: bool = True
    description: str = ""


class HowToUseDependency(Model):
    path: str
    description: str
    required: bool
    many: bool


class HowToUseDocumentation(Model):
    content_ref: URI
    markdown: str


class HowToUseOutput(Model):
    resource: URI
    mcue: MCUE
    summary: str
    usage_markdown: str
    attribute_help: list[HowToUseAttribute] = Field(default_factory=list)
    dependency_fields: list[HowToUseDependency] = Field(default_factory=list)
    actions: list[HowToUseAction] = Field(default_factory=list)
    documentation: HowToUseDocumentation | None = None
    sources: list[URI] = Field(default_factory=list)


class RunInput(Model):
    package: URI
    action: URI
    arguments: dict[str, Any]
    idempotency_key: str = Field(min_length=1, max_length=128)


from loom_v2.contracts.types import ActivationDependency


class ActivateInput(Model):
    package: URI
    idempotency_key: str = Field(min_length=1, max_length=128)
    instance_name: str = Field(default="default", pattern=r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
    instance_config: dict[str, Any] = Field(default_factory=dict)
    credential_profile: CredentialProfile | None = Field(
        default=None,
        description="Slave operator credential profile name for this activation, not a path or secret. "
                    "Omission/null preserves an existing binding or mounts no credentials for a new instance. "
                    "Select a new instance_name to change profiles.",
    )
    target: URI | None = None
    dependencies: dict[str, ActivationDependency] = Field(default_factory=dict)


class CheckPackageInput(Model):
    package: URI
    instance_name: str = Field(default="default", pattern=r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
    instance_config: dict[str, Any] | None = None


class ActivationIssue(Model):
    code: str
    interaction: str | None
    actions: list[URI]


class CheckPackageOutput(Model):
    ready: bool
    required_interactions: list[str]
    issues: list[ActivationIssue]


class CallInput(Model):
    arguments: dict[str, Any]
    expected_revision: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=128)


class RevisionInput(Model):
    expected_revision: int = Field(ge=1)


class StopInput(Model):
    expected_revision: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=128)


class ConfigureInput(StopInput):
    instance_config: dict[str, Any] | None = None
    dependencies: dict[str, ActivationDependency] | None = None


class ConfigureOutput(ResourceOutput):
    desired_revision: int
    applied_revision: int | None
    state: str


INPUTS = {"read": Empty, "cancel": Empty, "how-to-use": Empty, "list-resources": ListInput,
          "publish-mcue-type": McueDefinition, "update-attributes": UpdateAttributesInput,
          "register-resource": RegisterInput, "run-package": RunInput, "check-package": CheckPackageInput, "activate-package": ActivateInput, "stop": StopInput, "auth-start": RevisionInput, "configure": ConfigureInput}
OUTPUTS = {"how-to-use": HowToUseOutput, "check-package": CheckPackageOutput, "configure": ConfigureOutput,"publish-mcue-type": ResourceOutput, "register-resource": AttributesOutput,
           "update-attributes": AttributesOutput}
INPUTS.update(native_tasks.INPUTS)
OUTPUTS.update(native_tasks.OUTPUTS)
OBJECT = {"type": "object"}
RUN_RESULT = {"type": "object", "properties": {"run": {"type": "string"}},
             "required": ["run"], "additionalProperties": False}


@dataclass
class Entry:
    description: dict[str, Any]
    value: Any = None


@dataclass
class PackageActions:
    offers: list[dict]
    declarations: list[dict]
    entries: dict[str, Entry]


class GripService:
    def __init__(self, app, settings):
        self.app = app
        self.settings = settings
        self.names = Names(settings.grip_authority, settings.workspace_id)
        self._immutable_metadata = {}
        base = settings.public_base_url.rstrip("/")
        self.endpoint = base + "/.well-known/grip" if base else None
        self.protocol = "https-json.v1" if urlsplit(base).scheme == "https" else "loom-http-json.v1"
        if base and (urlsplit(base).scheme not in {"http", "https"} or urlsplit(base).username
                     or urlsplit(base).query or urlsplit(base).fragment):
            raise ValueError("invalid_grip_public_base_url")
        self._builtin = {name: self.offer(self.names.action(name), self.names.schema(name + "-input"),
                                        self.names.schema(name + "-output")) for name in INPUTS}
        self._builtin["how-to-use"]["action"]["description"] = (
            "Explain how to use this resource, its public attributes, relationships, and actions."
        )
        for name, description in native_tasks.DESCRIPTIONS.items():
            self._builtin[name]["action"]["description"] = description
        self.native = NativeTasks(self)
        self._static = self._static_entries()
        self._call_schema = CallInput.model_json_schema()
        self._package_actions: dict[tuple[str, str], PackageActions] = {}

    @property
    def repo(self):
        return self.app.state.repo

    def package_uri(self, package):
        return package.version_ref

    def activation_uri(self, package, activation):
        return self.names.name("activations", package.package_id, package.package_version,
                               activation.target_slave, activation.instance_name)

    def export_uri(self, package, export):
        ref = export.action_descriptor_ref
        return self.names.name("actions", "exports", package.package_id, package.package_version, resource_label(ref.resource_id), "sha256", ref.digest)

    def offer(self, action, input_schema, output_schema, *, mode="request-reply"):
        from loom_orchestration.bindings import stream_endpoint
        binding = {"protocol": "loom-wss-stream.v1" if mode == "byte-stream" else self.protocol, "audience": self.names.root}
        if self.endpoint:
            binding["endpoint"] = stream_endpoint(self.settings.public_base_url) if mode == "byte-stream" else self.endpoint
        return {"action": {"uri": action, "mode": mode,
                           "input_schema": input_schema, "output_schema": output_schema},
                "bindings": [binding]}

    async def _metadata(self, ref, *, descriptor=False):
        key = (ref.resource_id, ref.digest, descriptor)
        if key not in self._immutable_metadata:
            self._immutable_metadata[key] = (await self.repo.read_action_descriptor(ref) if descriptor
                                             else await self.repo._load_json_content(ref))
        return self._immutable_metadata[key]

    def _entry(self, target, mcue, metadata, offers, value=None):
        metadata = {"mcue_ref": self.names.mcue(mcue), "attributes": {}, "dependencies": None, **metadata}
        if all(offer["action"]["uri"] != self.names.action("how-to-use") for offer in offers):
            offers = [*offers, self._builtin["how-to-use"]]
        return Entry({"resource": {"uri": target, "mcue": mcue, "metadata": metadata}, "offers": offers}, value)

    def _schema_entry(self, target, schema):
        return self._entry(target, "grip/schema", {"language": "https://json-schema.org/draft/2020-12/schema"},
                           [self._builtin["read"]], schema)

    def _static_entries(self) -> dict[str, Entry]:
        n = self.names
        builtin = self._builtin
        entries = {n.root: self._entry(n.root, "loom/workspace", {},
                    [builtin["list-resources"], builtin["register-resource"], builtin["publish-mcue-type"]])}
        for name, model in INPUTS.items():
            entries[n.action(name)] = self._entry(n.action(name), "grip/action", {"contract": builtin[name]["action"]},
                                                  [builtin["read"]], builtin[name]["action"])
            entries[n.schema(name + "-input")] = self._schema_entry(n.schema(name + "-input"), model.model_json_schema())
            entries[n.schema(name + "-output")] = self._schema_entry(n.schema(name + "-output"),
                OUTPUTS[name].model_json_schema() if name in OUTPUTS
                else RUN_RESULT if name == "run-package" else OBJECT
            )
        entries[n.schema("run-output")] = self._schema_entry(n.schema("run-output"), RUN_RESULT)
        for name, model in (("closure-contract", ClosureContract), ("read-run-input", RunReadInput),
                            ("read-run-output", RunReadOutput), ("read-plan-output", PlanReadOutput)):
            entries[n.schema(name)] = self._schema_entry(n.schema(name), model.model_json_schema())
        return entries

    async def _actions(self, package) -> PackageActions:
        key = (package.version_ref, package.package_digest)
        if key in self._package_actions:
            return self._package_actions[key]
        n = self.names
        result = PackageActions([], [], {})
        for export in package.action_exports:
            action = self.export_uri(package, export)
            contract = await self._metadata(export.io_contract_ref)
            argument_schema = OBJECT
            if contract.get("input_schema_ref"):
                argument_schema = await self._metadata(ResourceRef.model_validate(contract["input_schema_ref"]))
            schema_uri = n.name("schemas", "calls", package.package_id, package.package_version,
                                resource_label(export.action_descriptor_ref.resource_id), "sha256", export.action_descriptor_ref.digest)
            call_kind = interaction(export)
            if call_kind == "task":
                schema = deepcopy(self._call_schema)
                schema["properties"]["arguments"] = argument_schema
                result.entries[schema_uri] = self._schema_entry(schema_uri, schema)
                offer = self.offer(action, schema_uri, n.schema("run-output"))
            else:
                output_schema = OBJECT
                if contract.get("output_schema_ref"):
                    output_schema = await self._metadata(ResourceRef.model_validate(contract["output_schema_ref"]))
                result.entries[schema_uri] = self._schema_entry(schema_uri, argument_schema)
                result.entries[schema_uri + "/output"] = self._schema_entry(schema_uri + "/output", output_schema)
                offer = self.offer(action, schema_uri, schema_uri + "/output", mode=call_kind)
            result.offers.append(offer)
            descriptor = await self._metadata(export.action_descriptor_ref, descriptor=True)
            if descriptor.get("description"):
                offer["action"]["description"] = descriptor["description"]
            declaration = {
                **offer["action"],
                "name": descriptor.get("name") or resource_label(export.action_descriptor_ref.resource_id),
                "description": descriptor.get("description", ""),
                "action_descriptor_ref": export.action_descriptor_ref.model_dump(mode="json", exclude_none=True),
                "io_contract_ref": export.io_contract_ref.model_dump(mode="json", exclude_none=True),
                "arguments_schema": contract.get("input_schema_ref"),
                "result_schema": contract.get("output_schema_ref"),
                "effect_class": export.effect_class,
                "permissions": list(export.permissions),
                "replay_safety": export.replay_safety,
                "descriptor": descriptor,
                "interaction": call_kind,
            }
            result.declarations.append(declaration)
            result.entries[action] = self._entry(action, "grip/action", declaration, [self._builtin["read"]], declaration)
        self._package_actions[key] = result
        return result

    def _target_family(self, target):
        for prefix, family in (("mcue-types", "grip/mcue-type"), ("slaves", "computing/slave"),
                               ("drivers", "computing/driver"), ("plans", "execution/plan"),
                               ("packages", "capability/package"), ("activations", "capability/activation"),
                               ("schemas", "grip/schema"), ("resources", "managed"), ("runs", "execution/run")):
            if target.startswith(self.names.root + "/" + prefix + "/"):
                return family
        # Descriptor identities are supplied by packages, not inferred from URI paths.
        return "grip/action"

    async def catalog(self, *, target: str | None = None, mcue: str | None = None) -> dict[str, Entry]:
        n, builtin = self.names, self._builtin
        entries: dict[str, Entry] = {}
        family = self._target_family(target) if target is not None else None

        def wanted(uri, resource_mcue):
            return (target is None or target == uri) and (mcue is None or mcue == resource_mcue)

        def source(resource_mcue):
            return (family is None or family == resource_mcue) and (mcue is None or mcue == resource_mcue)

        def add(uri, resource_mcue, metadata, offers, value=None):
            if wanted(uri, resource_mcue):
                entries[uri] = self._entry(uri, resource_mcue, metadata, offers, value)

        # Registered resources may use any MCUE, including a built-in type.
        definitions, resources = {}, []
        if target is None or family in {"grip/mcue-type", "managed"}:
            definitions = await self.repo.list_mcue_definitions(self.settings.workspace_id)
        if source("grip/mcue-type"):
            for uri, definition in definitions.items():
                add(uri, "grip/mcue-type", {"mcue": definition["mcue"], "version": definition["version"]},
                    [builtin["read"]], definition)
        if target is None or family == "managed":
            resources = [r for r in await self.repo.list_resources(self.settings.workspace_id)
                         if wanted(r.resource_ref.resource_id, definitions[r.mcue_ref]["mcue"])]

        if source("computing/slave"):
            agents = await self.repo.list_agents(self.settings.workspace_id, role="slave")
            from loom_v2.contracts.refs import select_slave_agents
            agents.sort(key=lambda a: str(a.get("last_seen_at") or ""), reverse=True)
            for slave, agent in select_slave_agents(agents).items():
                caps = agent.get("capabilities") or {}
                public = caps.get("resource_metadata") or {"mcue_ref": n.mcue("computing/slave"), "attributes": {}}
                definition = await self.repo.get_mcue_definition(self.settings.workspace_id, public["mcue_ref"])
                add(n.name("slaves", slave), "computing/slave", {
                    **public,
                    "dependencies": resource_dependencies(definition.attributes_schema, public["attributes"]),
                    "slave_id": slave,
                    "available": agent["lease_state"] == "active",
                    "lease_state": agent["lease_state"], "last_seen_at": agent.get("last_seen_at"),
                    "base_actions": sorted(caps.get("base_actions", [])),
                    "term_support": caps.get("term_support", []),
                    "executor_descriptors": caps.get("executor_descriptors", []),
                    "runtime_plugin_descriptors": caps.get("runtime_plugin_descriptors", []),
                    "service_interactions": self.repo.service_routes.node_metadata(slave),
                }, [builtin["run-package"], builtin["check-package"], builtin["activate-package"]], slave)

        states = []
        if resources or any(source(t) for t in ("capability/package", "capability/activation", "grip/action", "grip/schema")):
            package_ref = target if family == "capability/package" else None
            if target is not None:
                for prefix in ("activations/", "schemas/calls/", "actions/exports/"):
                    path = n.root + "/" + prefix
                    if target.startswith(path):
                        parts = target[len(path):].split("/")
                        if len(parts) >= 2:
                            package_ref = n.name("packages", unquote(parts[0]), unquote(parts[1]))
            states = await self.repo.list_published_package_states(self.settings.workspace_id, package_ref=package_ref)
        resource_ids = {r.resource_ref.resource_id for r in resources}
        bound: dict[str, list[tuple[Any, Any, list]]] = {}
        for package, activations in states:
            package_uri = self.package_uri(package)
            selected = [a for a in activations if wanted(self.activation_uri(package, a), "capability/activation")]
            bindings = [a for a in activations if a.activation_state == "ready" and a.desired_state == "running"
                        and a.target_resource_ref and a.target_resource_ref.resource_id in resource_ids]
            if not (wanted(package_uri, "capability/package") or selected or bindings or source("grip/action") or source("grip/schema")):
                continue
            actions = await self._actions(package)
            exports, declarations = actions.offers, actions.declarations
            for uri, entry in actions.entries.items():
                if wanted(uri, entry.description["resource"]["mcue"]):
                    entries[uri] = entry
            for declaration in declarations:
                identity = declaration["descriptor"].get("resource_id")
                if identity and wanted(identity, "grip/action"):
                    if identity not in entries:
                        revisions = {"descriptors": []}
                        add(identity, "grip/action", revisions, [builtin["read"]], revisions)
                    ref = declaration["action_descriptor_ref"]
                    if ref not in entries[identity].value["descriptors"]:
                        entries[identity].value["descriptors"].append(ref)
            add(package_uri, "capability/package", {
                "package_id": package.package_id, "package_version": package.package_version,
                "digest": package.package_digest, "execution": package.execution.model_dump(mode="json"),
                "executable": package.is_executable, "actions": declarations,
            }, [builtin["read"]], package)
            for activation in selected:
                activation_uri = self.activation_uri(package, activation)
                bound_target = (activation.target_resource_ref.resource_id
                          if activation.target_resource_ref else None)
                ready = activation.activation_state == "ready" and activation.desired_state == "running"
                projected = self.app.state.service_access.project(package, activation, exports) if ready else []
                callable_actions = {o["action"]["uri"] for o in projected}
                unavailable_reason = (None if ready else
                                      "desired_state_" + activation.desired_state if activation.desired_state != "running" else
                                      "activation_" + activation.activation_state)
                config_schema = await activation_config_schema(self.repo, package)
                metadata = {"package": package_uri, "host": n.name("slaves", activation.target_slave),
                            "dependencies": resource_dependencies(config_schema, activation.instance_config) if config_schema else None,
                            "name": package.package_id + ":" + activation.instance_name,
                            "description": f"Activated {package.package_id} {package.package_version} on {activation.target_slave}.",
                            "instance_name": activation.instance_name, **self.repo.service_routes.status(package, activation),
                            "desired_state": activation.desired_state, "revision": activation.activation_revision,
                            "applied_revision": activation.applied_revision,
                            "target": bound_target,
                            **self.app.state.service_access.metadata(package, activation),
                            "actions": [{**a, "available": a["uri"] in callable_actions, "unavailable_reason": None if a["uri"] in callable_actions else unavailable_reason or "binding_unavailable"}
                                        for a in declarations]}
                authentication = (package.body or {}).get("authentication")
                if authentication:
                    metadata["authentication"] = authentication_view(
                        package, activation.runtime_profile.get("authentication") or {})
                add(activation_uri, "capability/activation", metadata,
                    [builtin["stop"], *([builtin["configure"]] if activation.desired_state == "running" else []), *([builtin["auth-start"]] if authentication and authentication.get("interaction") and activation.desired_state == "running" else []), *projected], (package, activation))
            for activation in bindings:
                bound.setdefault(activation.target_resource_ref.resource_id, []).append((package, activation, self.app.state.service_access.project(package, activation, exports)))

        package_names = {p.version_ref: self.package_uri(p) for p, _ in states}
        for resource in resources:
            uri = resource.resource_ref.resource_id
            candidates = bound.get(uri, [])
            # Ambiguous instances are named explicitly, never chosen by order.
            offers = candidates[0][2] if len(candidates) == 1 else []
            add(uri, definitions[resource.mcue_ref]["mcue"], {
                "mcue_ref": resource.mcue_ref,
                "attributes": resource.attributes,
                "attributes_revision": resource.attributes_revision,
                "dependencies": resource_dependencies(definitions[resource.mcue_ref]["attributes_schema"], resource.attributes),
                "state": resource.state,
                "package": package_names.get(resource.package_ref.resource_id) if resource.package_ref else None,
                "activations": [self.activation_uri(p, a) for p, a, _ in candidates],
                "managed_by": resource.managed_by_ref.resource_id if resource.managed_by_ref else None,
                **(self.app.state.service_access.metadata(*candidates[0][:2]) if len(candidates) == 1 else {}),
            }, [builtin["update-attributes"], *offers], candidates[0][:2] if len(candidates) == 1 else None)
        plan_summaries = None
        if source("execution/run"):
            run_id = unquote(target[len(n.root + "/runs/"):]) if target is not None else None
            plan_summaries = await self.repo.list_plan_summaries(self.settings.workspace_id, run_id=run_id)
            plan_versions = {(plan.run_id, plan.closure_version) for plan in plan_summaries}
            for record in await self.repo.list_run_summaries(self.settings.workspace_id, run_id=run_id):
                add(n.name("runs", record.run_id), "execution/run",
                    self.native.run_metadata(record, has_plan=(record.run_id, record.closure_version) in plan_versions),
                    [self.offer(n.action("read"), n.schema("read-run-input"), n.schema("read-run-output")),
                     *[builtin[name] for name in ("cancel", "resubmit", "resolve", "close")]], record)
        if source("computing/driver") and wanted(self.native.driver_uri, "computing/driver"):
            add(self.native.driver_uri, "computing/driver", await self.native.driver_metadata(),
                [builtin["submit-plan"], builtin["get-submission"]])
        if source("execution/plan"):
            parts = target[len(n.root + "/plans/"):].split("/") if target is not None else None
            if parts is None or len(parts) == 2:
                plans = plan_summaries if plan_summaries is not None else await self.repo.list_plan_summaries(self.settings.workspace_id,
                    run_id=unquote(parts[0]) if parts else None,
                    closure_version=unquote(parts[1]) if parts else None)
                for plan in plans:
                    add(self.native.plan_uri(plan.run_id, plan.closure_version), "execution/plan",
                        {"run": n.name("runs", plan.run_id), "closure_version": plan.closure_version,
                         "snapshot_digest": plan.snapshot_digest, "schema": n.schema("closure-contract")},
                        [self.offer(n.action("read"), n.schema("read-input"), n.schema("read-plan-output"))], plan)
        entries.update({uri: entry for uri, entry in self._static.items() if wanted(uri, entry.description["resource"]["mcue"])})
        return entries

    async def resolve(self, target: str) -> Entry:
        if target in self._static:
            return self._static[target]
        if target.startswith(self.names.root + "/content/") and content_digest(target):
            entries = {}
            await self._add_content(target, entries)
        else:
            entries = await self.catalog(target=target)
        if target not in entries:
            raise GripError("not_found")
        return entries[target]

    async def _add_content(self, target, catalog):
        if not target.startswith(self.names.root + "/content/"):
            raise GripError("not_found")
        ref = ResourceRef(resource_id=target, identity_criterion="content_digest")
        stat = await self.repo.content_store.stat(ref)
        if stat is None:
            raise GripError("not_found")
        raw = await self.repo.content_store.get(ref)
        if stat.media_type == "application/json" or stat.media_type.endswith("+json"):
            value = json.loads(raw)
            encoding = "json"
        elif stat.media_type.startswith("text/"):
            value = raw.decode("utf-8")
            encoding = "utf-8"
        else:
            value = base64.b64encode(raw).decode("ascii")
            encoding = "base64"
        catalog[target] = Entry({
            "resource": {"uri": target, "mcue": "data/content", "metadata": {
                "mcue_ref": self.names.mcue("data/content"), "attributes": {},
                "media_type": stat.media_type, "sha256": ref.digest, "size": stat.size,
            }},
            "offers": [self.offer(self.names.action("read"), self.names.schema("read-input"), self.names.schema("read-output")),
                       self._builtin["how-to-use"]],
        }, {"content": value, "media_type": stat.media_type, "encoding": encoding})

    @staticmethod
    def _attribute_help(schema: dict[str, Any], prefix: str = "") -> list[dict[str, Any]]:
        properties = schema.get("properties", {})
        if not isinstance(properties, dict):
            return []
        required = set(schema.get("required", []))
        result = []
        for name, definition in properties.items():
            if not isinstance(definition, dict):
                continue
            path = f"{prefix}.{name}" if prefix else name
            value_type = definition.get("type")
            if not isinstance(value_type, (str, list)):
                value_type = None
            result.append({"name": path, "type": value_type, "required": name in required,
                           "description": str(definition.get("description") or "")})
            if value_type == "object":
                result.extend(GripService._attribute_help(definition, path))
        return result

    async def _how_to_use(self, target: str, entry: Entry) -> dict[str, Any]:
        resource = entry.description["resource"]
        metadata = resource.get("metadata", {})
        mcue = resource["mcue"]
        attributes = metadata.get("attributes", {})
        if not isinstance(attributes, dict):
            attributes = {}
        attribute_help = []
        dependencies_help = []
        mcue_ref = metadata.get("mcue_ref")
        if isinstance(mcue_ref, str):
            try:
                definition = await self.repo.get_mcue_definition(self.settings.workspace_id, mcue_ref)
                attribute_help = self._attribute_help(definition.attributes_schema)
                dependencies_help = [{**field, "path": "attributes." + field["path"]}
                                     for field in dependency_fields(definition.attributes_schema)]
            except (KeyError, ValueError):
                attribute_help = []

        documentation = None
        package = entry.value if isinstance(entry.value, CapabilityPackageVersion) else None
        dependency_package = package or (entry.value[0] if mcue == "capability/activation" else None)
        if dependency_package is not None:
            config_schema = await activation_config_schema(self.repo, dependency_package)
            if config_schema:
                dependencies_help.extend({**field, "path": "instance_config." + field["path"]}
                                         for field in dependency_fields(config_schema))
            for export in dependency_package.action_exports:
                descriptor = await self.repo.read_action_descriptor(export.action_descriptor_ref)
                for index, effect in enumerate(descriptor.get("resource_effects", [])):
                    if "mcue_ref" not in effect:
                        continue
                    definition = await self.repo.get_mcue_definition(self.settings.workspace_id, effect["mcue_ref"])
                    action_name = descriptor.get("name", export.action_descriptor_ref.resource_id)
                    dependencies_help.extend({**field, "path": f"{action_name}.resource_effects.{index}.attributes." + field["path"]}
                                             for field in dependency_fields(definition.attributes_schema))
        if package is not None and package.how_to_use_ref is not None:
            try:
                raw = await self.repo.content_store.get(package.how_to_use_ref)
                documentation = {"content_ref": package.how_to_use_ref.resource_id,
                                 "markdown": raw.decode("utf-8")}
            except (UnicodeDecodeError, FileNotFoundError, TypeError, ValueError):
                documentation = None

        actions = []
        source_refs = [target]
        if isinstance(mcue_ref, str):
            source_refs.append(mcue_ref)
        for offer in entry.description.get("offers", []):
            action = offer.get("action", {})
            action_uri = action.get("uri")
            if not isinstance(action_uri, str) or action_uri == self.names.action("how-to-use"):
                continue
            input_schema = action.get("input_schema")
            output_schema = action.get("output_schema")
            if not isinstance(input_schema, str) or not isinstance(output_schema, str):
                continue
            actions.append({"action": action_uri, "mode": action.get("mode", "request-reply"),
                            "input_schema": input_schema, "output_schema": output_schema,
                            "available": bool(offer.get("available", True)),
                            "description": str(action.get("description") or "")})
            source_refs.append(action_uri)

        # A package resource advertises its exported actions in metadata rather
        # than as callable offers. Include those declarations so an agent can
        # understand what must be activated before invoking the action.
        if mcue == "capability/package" and isinstance(metadata.get("actions"), list):
            actions = []
            for declaration in metadata["actions"]:
                if not isinstance(declaration, dict):
                    continue
                action_uri = declaration.get("uri")
                input_schema = declaration.get("input_schema")
                output_schema = declaration.get("output_schema")
                if not all(isinstance(value, str) for value in (action_uri, input_schema, output_schema)):
                    continue
                actions.append({"action": action_uri, "mode": declaration.get("mode", "request-reply"),
                                "input_schema": input_schema, "output_schema": output_schema,
                                "available": False,
                                "description": str(declaration.get("description") or "")})
                source_refs.append(action_uri)

        label = (attributes.get("display_name") or metadata.get("name") or
                 metadata.get("package_id") or mcue)
        summary = f"{mcue} resource: {label}"
        lines = [f"Use `{target}` as a `{mcue}` resource."]
        if mcue in native_tasks.USAGE:
            lines.append(native_tasks.USAGE[mcue])
        lines.append("Read current attributes and dependency references with DESCRIBE or the workspace list-resources action.")
        if dependencies_help:
            lines.append("Dependency fields (fill them according to the declared schema and creation mappings):")
            for field in dependencies_help:
                lines.append(f"- `{field['path']}`: {field['description']}")
        if actions:
            lines.append("Declared actions (activate the package before calling them):" if mcue == "capability/package" else "Available actions:")
            for action in actions:
                state = "available" if action["available"] else "unavailable"
                lines.append(f"- `{action['action']}` ({action['mode']}, {state}); input schema `{action['input_schema']}`, output schema `{action['output_schema']}`.")
        else:
            lines.append("No callable business action is currently advertised; use the returned metadata and schemas to determine the next step.")
        return HowToUseOutput(
            resource=target, mcue=mcue, summary=summary,
            usage_markdown="\n".join(lines) + (("\n\n" + documentation["markdown"]) if documentation else ""),
            attribute_help=attribute_help,
            dependency_fields=dependencies_help,
            actions=actions,
            documentation=documentation,
            sources=list(dict.fromkeys(source_refs + ([documentation["content_ref"]] if documentation else []))),
        ).model_dump(mode="json")

    async def _package(self, target):
        entry = await self.resolve(target)
        if not isinstance(entry.value, CapabilityPackageVersion):
            raise GripError("not_found")
        package = entry.value
        if package.is_interface or package.execution.kind == "container:python_orchestrator":
            raise GripError("invalid_input")
        return package

    def _export(self, package, action):
        for export in package.action_exports:
            if self.export_uri(package, export) == action:
                return export
        raise GripError("unsupported_action")

    async def _submit(self, *, target, slave, package, export, arguments, key, activation=None):
        if interaction(export) == "byte-stream":
            raise GripError("unsupported_action")
        # submit_run_plan resolves idempotency before performing current
        # readiness checks, so a retry still finds its task if a Slave went away.
        contract = await self._metadata(export.io_contract_ref)
        if contract.get("input_schema_ref"):
            schema = await self._metadata(ResourceRef.model_validate(contract["input_schema_ref"]))
            if not Draft202012Validator(schema).is_valid(arguments):
                raise GripError("invalid_input")
        input_ref = await self.repo.put_content(arguments, media_type="application/json")
        # Input idempotency is application state, independent of request_id.
        submission_key = "grip-" + hashlib.sha256(key.encode()).hexdigest()
        action = export.action_descriptor_ref.resource_id
        binding = {
            "binding_id": "grip-binding", "hole_id": "grip-target",
            "action_descriptor_ref": export.action_descriptor_ref,
            "capability_package_ref": ResourceRef(resource_id=package.version_ref, version_or_digest=package.package_digest),
            "target_resource_ref": ResourceRef(resource_id=self.names.name("slaves", slave)),
            "bound_by": "grip",
        }
        if activation is not None:
            binding.update(instance_name=activation.instance_name, expected_activation_revision=activation.activation_revision)
        body = TaskClosure(
            closure_id=submission_key,
            program={"action_ref": action, "io_contract_ref": export.io_contract_ref},
            compute={"action_ref": action, "typed_holes": [{"hole_id": "grip-target", "status": "bound", "binding_ref": "grip-binding"}]},
            compute_bindings=[binding], node_input_bindings=[{"node_id": action, "input_ref": input_ref}],
            metadata={"grip_target": target, "grip_action": self.export_uri(package, export)},
        )
        plan = ClosureContract(closure_id=submission_key, goal="Execute GRIP resource action",
                               workspace_id=self.settings.workspace_id, allowed_effects=list(export.permissions),
                               recovery_policy={"allow_reassignment": False}, body=body)
        queued = await self.repo.submit_run_plan(workspace_id=self.settings.workspace_id,
                                               request_id=submission_key, plan=plan)
        return {"run": self.names.name("runs", queued.run_id)}

    async def invoke(self, request, entry, http=None):
        offer = next((o for o in entry.description["offers"] if o["action"]["uri"] == request.action), None)
        if offer is None:
            raise GripError("unsupported_action")
        schema = (await self.resolve(offer["action"]["input_schema"])).value
        if not Draft202012Validator(schema).is_valid(request.input):
            raise GripError("invalid_input")
        n = self.names
        data = request.input
        if request.action == n.action("list-resources"):
            return {"resources": [e.description["resource"] for e in (await self.catalog(mcue=data.get("mcue"))).values()]}
        if request.action == n.action("how-to-use"):
            return await self._how_to_use(request.target, entry)
        family = entry.description["resource"]["mcue"]
        if family == "computing/driver" and request.target == self.native.driver_uri:
            for action in ("submit-plan", "get-submission"):
                if request.action == n.action(action):
                    return await self.native.invoke(action, data)
        if isinstance(entry.value, RunSummary):
            if request.action == n.action("read"):
                payload = RunReadInput.model_validate(data)
                return await self.native.read_run(entry.value.run_id, include_history=payload.include_history)
            for action in ("cancel", "resubmit", "resolve", "close"):
                if request.action == n.action(action):
                    return await self.native.invoke(action, data, run_id=entry.value.run_id)
        if family == "execution/plan" and request.action == n.action("read"):
            plan = entry.value
            return {"run": n.name("runs", plan.run_id), "closure_version": plan.closure_version,
                    "snapshot_digest": plan.snapshot_digest,
                    "plan": await self.repo.read_frozen_plan(self.settings.workspace_id, plan.run_id, plan.closure_version)}
        if request.action == n.action("publish-mcue-type"):
            target = await self.repo.publish_mcue_type(self.settings.workspace_id, data)
            return {"resource": target}
        if request.action == n.action("update-attributes"):
            payload = UpdateAttributesInput.model_validate(data)
            resource = await self.repo.update_resource_attributes(
                self.settings.workspace_id, request.target, **payload.model_dump(exclude_none=True),
            )
            return {"resource": request.target, "attributes_revision": resource.attributes_revision}
        if request.action == n.action("register-resource"):
            payload = RegisterInput.model_validate(data)
            package_ref = None
            if payload.package is not None:
                package_entry = await self.resolve(payload.package)
                if not isinstance(package_entry.value, CapabilityPackageVersion):
                    raise GripError("not_found")
                package = package_entry.value
                package_ref = ResourceRef(resource_id=package.version_ref, version_or_digest=package.package_digest)
            manager = None
            if payload.managed_by:
                for resource in await self.repo.list_resources(self.settings.workspace_id):
                    if resource.resource_ref.resource_id == payload.managed_by:
                        manager = resource.resource_ref
                        break
                if manager is None:
                    raise GripError("not_found")
            resource = await self.repo.register_resource(self.settings.workspace_id, {
                "resource_ref": ResourceRef(resource_id=payload.resource_id),
                "mcue_ref": payload.mcue_ref,
                **({"package_ref": package_ref} if "package" in payload.model_fields_set else {}),
                **({"managed_by_ref": manager} if "managed_by" in payload.model_fields_set else {}),
                **({"state": payload.state} if "state" in payload.model_fields_set else {}),
                **({"attributes": payload.attributes} if "attributes" in payload.model_fields_set else {}),
            })
            return {"resource": resource.resource_ref.resource_id, "attributes_revision": resource.attributes_revision}
        if request.action == n.action("read"):
            if isinstance(entry.value, CapabilityPackageVersion):
                return {**entry.value.model_dump(mode="json"), "resource": entry.value.version_ref}
            return entry.value
        if request.action == n.action("run-package"):
            payload = RunInput.model_validate(data)
            package = await self._package(payload.package)
            export = self._export(package, payload.action)
            return await self._submit(target=request.target, slave=entry.value, package=package,
                                      export=export, arguments=payload.arguments, key=payload.idempotency_key)
        if request.action == n.action("check-package"):
            payload = CheckPackageInput.model_validate(data)
            package = await self._package(payload.package)
            await self.repo.refresh_slaves(self.settings.workspace_id)
            issues = self.repo.service_routes.issues(package, entry.value, payload.instance_name)
            if not self.repo._slave_is_active(entry.value):
                issues.append({"code": "slave_unavailable", "interaction": None, "actions": []})
            elif not self.repo._slave_supports_package(entry.value, package):
                issues.append({"code": "package_runtime_unsupported", "interaction": None, "actions": []})
            activations = await self.repo.list_package_activations(package)
            current = next((a for a in activations if a.target_slave == entry.value and a.instance_name == payload.instance_name), None)
            config = payload.instance_config if payload.instance_config is not None else (current.instance_config if current else {})
            try:
                await validate_resource_dependencies(self.repo, package)
                await validate_activation_config(self.repo, package, config)
            except ValueError as exc:
                issues.append({"code": str(exc), "interaction": None, "actions": []})
            return {"ready": not issues,
                    "required_interactions": sorted({interaction(e) for e in package.action_exports}), "issues": issues}
        if request.action == n.action("activate-package"):
            payload = ActivateInput.model_validate(data)
            package = await self._package(payload.package)
            await self.repo.refresh_slaves(self.settings.workspace_id)
            if not self.repo._slave_supports_package(entry.value, package):
                raise GripError("invalid_input")
            target_ref = None
            if payload.target:
                for resource in await self.repo.list_resources(self.settings.workspace_id):
                    if resource.resource_ref.resource_id == payload.target:
                        target_ref = resource.resource_ref
                        break
                if target_ref is None:
                    raise GripError("not_found")
            await self._activate(package, entry.value, "running", payload.idempotency_key,
                                 payload.instance_name, target_ref,
                                 payload.instance_config if "instance_config" in payload.model_fields_set else None,
                                 credential_profile=payload.credential_profile,
                                 dependencies=payload.dependencies if "dependencies" in payload.model_fields_set else None)
            activations = await self.repo.list_package_activations(package)
            activation = next(a for a in activations if a.target_slave == entry.value and a.instance_name == payload.instance_name)
            return {"resource": self.activation_uri(package, activation), "state": self.repo.service_routes.status(package, activation)["state"],
                    "revision": activation.activation_revision}
        package, activation = entry.value
        if request.action == n.action("configure"):
            payload = ConfigureInput.model_validate(data)
            if not ({"instance_config", "dependencies"} & payload.model_fields_set) or any(data.get(k) is None for k in ("instance_config", "dependencies") if k in data):
                raise GripError("invalid_input")
            if payload.dependencies is not None:
                await self.app.state.service_access.validate_dependencies(payload.dependencies)
            await self._activate(package, activation.target_slave, "running", payload.idempotency_key,
                activation.instance_name, activation.target_resource_ref, payload.instance_config,
                credential_profile=activation.credential_profile, expected_revision=payload.expected_revision,
                dependencies=payload.dependencies, configure=True)
            current = next(a for a in await self.repo.list_package_activations(package)
                if a.target_slave == activation.target_slave and a.instance_name == activation.instance_name)
            return {"resource": request.target, "desired_revision": current.activation_revision,
                    "applied_revision": current.applied_revision, "state": self.repo.service_routes.status(package, current)["state"]}
        if request.action not in {n.action("stop"), n.action("auth-start")}:
            export = self._export(package, request.action)
            if interaction(export) != "task":
                return await self.app.state.service_access.invoke(request)
        stop_retry = (request.action == n.action("stop") and activation.desired_state == "stopped"
                      and activation.last_idempotency_key == data["idempotency_key"]
                      and data["expected_revision"] == activation.activation_revision - 1)
        if data["expected_revision"] != activation.activation_revision and not stop_retry:
            raise GripError("invalid_input")
        if request.action == n.action("auth-start"):
            authentication = (package.body or {}).get("authentication") or {}
            if not authentication.get("interaction"):
                raise GripError("unsupported_action")
            if authentication.get("browser_path"):
                auth = await self.app.state.browser_auth.start(http, package, [activation.target_slave], activation.instance_name, expected_revision=data["expected_revision"])
                return {"resource": request.target, "revision": activation.activation_revision, "authentication": auth}
            reports = await execute_auth_targets(
                self.repo, package, [activation.target_slave], workspace_id=self.settings.workspace_id,
                workers=self.app.state.workers, slaves=self.app.state.slaves,
                approved_digest=package.package_digest, instance_name=activation.instance_name,
                slave_gateway=None if self.app.state.embedded else self.app.state.slave_gateway,
                activation_revision=activation.activation_revision,
            )
            report, = reports
            if report.get("error"):
                raise GripError("execution_failed")
            auth = authentication_view(package, report.get("details", {}).get("authentication") or {})
            return {"resource": request.target, "revision": activation.activation_revision, "authentication": auth}
        if request.action == n.action("stop"):
            await self._activate(package, activation.target_slave, "stopped", data["idempotency_key"],
                                 activation.instance_name, activation.target_resource_ref, activation.instance_config,
                                 credential_profile=activation.credential_profile,
                                 expected_revision=data["expected_revision"])
            current = next(a for a in await self.repo.list_package_activations(package)
                           if a.target_slave == activation.target_slave and a.instance_name == activation.instance_name)
            return {"resource": request.target, "state": current.activation_state, "revision": current.activation_revision}
        export = self._export(package, request.action)
        return await self._submit(target=request.target, slave=activation.target_slave, package=package,
                                  export=export, arguments=data["arguments"], key=data["idempotency_key"], activation=activation)

    async def _activate(self, package, slave, state, key, instance, target, config, *, credential_profile=None, expected_revision=None, dependencies=None, configure=False):
        if state == "running":
            self.repo.service_routes.require(package, slave, instance)
        if dependencies is not None:
            await self.app.state.service_access.validate_dependencies(dependencies)
        reports = await execute_activation_targets(
            self.repo, package, [slave], desired_state=state, workspace_id=self.settings.workspace_id,
            workers=self.app.state.workers, slaves=self.app.state.slaves, idempotency_key=key,
            instance_name=instance, target_resource_ref=target, instance_config=config, expected_revision=expected_revision,
            credential_profile=credential_profile, dependencies=dependencies, configure=configure,
            slave_gateway=None if self.app.state.embedded else self.app.state.slave_gateway,
        )
        if not configure and any("error" in report for report in reports):
            raise GripError("internal_error")

    async def handle(self, http: HttpRequest):
        request_id = None
        try:
            raw = decode(await http.body())
            if not isinstance(raw, dict) or not isinstance(raw.get("request_id"), str) or not REQUEST_ID.fullmatch(raw["request_id"]):
                return JSONResponse(status_code=400, content={"error": "invalid_message"})
            request_id = raw["request_id"]
            if "version" not in raw:
                raise GripError("invalid_message")
            if raw["version"] != "grip.v1":
                raise GripError("unsupported_version")
            if "auth" not in raw:
                raise GripError("authentication_required")
            request = Request.model_validate(raw)
            owner = bool(self.settings.grip_token and hmac.compare_digest(request.auth.credential.encode(), self.settings.grip_token.encode()))
            if not owner:
                if request.method != "INVOKE":
                    raise GripError("authentication_failed")
                self.app.state.service_access.tokens.verify(request, self.names.root)
            if request.auth.tenant is not None and request.auth.tenant != self.names.root:
                raise GripError("forbidden")
            if self.protocol not in request.accepted_protocols:
                raise GripError("unsupported_binding")
            if request.deadline and datetime.fromisoformat(request.deadline.replace("Z", "+00:00")) <= datetime.now(timezone.utc):
                raise GripError("deadline_exceeded")
            entry = await self.resolve(request.target)
            if request.deadline and datetime.fromisoformat(request.deadline.replace("Z", "+00:00")) <= datetime.now(timezone.utc):
                raise GripError("deadline_exceeded")
            if request.method == "DESCRIBE":
                body = {**entry.description, "revision": digest_json(entry.description, domain="loom/grip-description/v1")}
                if body["resource"]["metadata"].get("service_access"):
                    body["expires_at"] = (datetime.now(timezone.utc) + timedelta(seconds=self.settings.grip_description_cache_seconds)).isoformat()
                Description.model_validate(body)
            else:
                if not owner:
                    # Scoped credentials cannot invoke unrelated control actions.
                    await self.app.state.service_access.resolve(request.target, request.action)
                body = await self.invoke(request, entry, http)
            return JSONResponse({"version": "grip.v1", "request_id": request_id, "kind": "reply", "body": body},
                                headers={"Cache-Control": "private, no-cache"})
        except GripError as exc:
            code = exc.code
        except ValidationError:
            code = "invalid_message"
        except ValueError as exc:
            public_code = str(exc).split(":", 1)[0]
            code = public_code if public_code in native_tasks.PUBLIC_ERRORS | {
                "attributes_revision_conflict", "mcue_version_conflict", "resource_mcue_conflict",
                "resource_attributes_require_update", "mcue_attributes_invalid",
                "resource_mcue_migration_required",
                "dependency_reference_invalid", "activation_config_invalid", "activation_config_field_unknown",
                "resource_attribute_source_missing", "resource_attribute_value_invalid",
                "resource_attributes_mapping_required",
            } else "invalid_input" if request_id else "invalid_message"
        except (TypeError, UnicodeError):
            code = "invalid_input" if request_id else "invalid_message"
        except (KeyError, FileNotFoundError):
            code = "not_found"
        except DomainError as exc:
            code = (exc.envelope.code if exc.envelope.code in native_tasks.PUBLIC_ERRORS else
                    "unsupported_binding" if exc.envelope.code == "activation_preflight_failed" else "invalid_input")
        except Exception:
            # Never include credentials, runtime details or exception payloads.
            log.error("GRIP action failed (request_id=%s)", request_id)
            code = "internal_error"
        if request_id is None:
            return JSONResponse(status_code=400, content={"error": code})
        return JSONResponse({"version": "grip.v1", "request_id": request_id, "kind": "error", "error_code": code})
