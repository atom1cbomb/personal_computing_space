"""Workspace resource directory and GRIP application actions.

The directory is a projection of existing authoritative records. Invocation
submits ordinary durable runs; it never introduces a second execution engine.
"""
from __future__ import annotations

import json
import base64
import hashlib
import hmac
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

from fastapi import Request as HttpRequest
from fastapi.responses import JSONResponse
from jsonschema import Draft202012Validator
from pydantic import Field, ValidationError

from loom_orchestration.grip import (
    URI, MCUE, Model, Names, Request, Description, GripError, REQUEST_ID, decode,
)
from loom_orchestration.contracts import CredentialProfile
from loom_orchestration.mcue import McueDefinition
from loom_orchestration.naming import content_digest, resource_label
from loom_v2.contracts.errors import DomainError
from loom_v2.contracts.types import CapabilityPackageVersion, ClosureContract, ResourceRef, TaskClosure
from loom_v2.observer.repository import RunRecord
from loom_v2.observer.activation import execute_activation_targets, execute_auth_targets
from loom_v2.digest import digest_json

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


class RunInput(Model):
    package: URI
    action: URI
    arguments: dict[str, Any]
    idempotency_key: str = Field(min_length=1, max_length=128)


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


class CallInput(Model):
    arguments: dict[str, Any]
    expected_revision: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=128)


class RevisionInput(Model):
    expected_revision: int = Field(ge=1)


class StopInput(Model):
    expected_revision: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=128)


INPUTS = {"read": Empty, "cancel": Empty, "list-resources": ListInput,
          "publish-mcue-type": McueDefinition, "update-attributes": UpdateAttributesInput,
          "register-resource": RegisterInput, "run-package": RunInput, "activate-package": ActivateInput, "stop": StopInput, "auth-start": RevisionInput}
OUTPUTS = {"publish-mcue-type": ResourceOutput, "register-resource": AttributesOutput,
           "update-attributes": AttributesOutput}
OBJECT = {"type": "object"}
RUN_RESULT = {"type": "object", "properties": {"run": {"type": "string"}},
             "required": ["run"], "additionalProperties": False}


@dataclass
class Entry:
    description: dict[str, Any]
    value: Any = None


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

    def offer(self, action, input_schema, output_schema):
        binding = {"protocol": self.protocol, "audience": self.names.root}
        if self.endpoint:
            binding["endpoint"] = self.endpoint
        return {"action": {"uri": action, "mode": "request-reply",
                           "input_schema": input_schema, "output_schema": output_schema},
                "bindings": [binding]}

    async def _metadata(self, ref, *, descriptor=False):
        key = (ref.resource_id, ref.digest, descriptor)
        if key not in self._immutable_metadata:
            self._immutable_metadata[key] = (await self.repo.read_action_descriptor(ref) if descriptor
                                             else await self.repo._load_json_content(ref))
        return self._immutable_metadata[key]

    async def catalog(self) -> dict[str, Entry]:
        n = self.names
        entries: dict[str, Entry] = {}
        schemas: dict[str, dict] = {}
        builtin = {}

        definitions = await self.repo.list_mcue_definitions(self.settings.workspace_id)

        def add(target, mcue, metadata, offers, value=None):
            metadata = {"mcue_ref": n.mcue(mcue), "attributes": {}, **metadata}
            description = {"resource": {"uri": target, "mcue": mcue, "metadata": metadata}, "offers": offers}
            entries[target] = Entry(description, value)

        for name, model in INPUTS.items():
            schemas[n.schema(name + "-input")] = model.model_json_schema()
            schemas[n.schema(name + "-output")] = (
                OUTPUTS[name].model_json_schema() if name in OUTPUTS
                else RUN_RESULT if name == "run-package" else OBJECT
            )
            builtin[name] = self.offer(n.action(name), n.schema(name + "-input"), n.schema(name + "-output"))
        schemas[n.schema("run-output")] = RUN_RESULT
        add(n.root, "loom/workspace", {}, [builtin["list-resources"], builtin["register-resource"], builtin["publish-mcue-type"]])
        for target, definition in definitions.items():
            add(target, "grip/mcue-type", {"mcue": definition["mcue"], "version": definition["version"]},
                [builtin["read"]], definition)
        agents = await self.repo.list_agents(self.settings.workspace_id, role="slave")
        from loom_v2.contracts.refs import select_slave_agents
        agents.sort(key=lambda a: str(a.get("last_seen_at") or ""), reverse=True)
        for slave, agent in select_slave_agents(agents).items():
            caps = agent.get("capabilities") or {}
            add(n.name("slaves", slave), "computing/slave", {
                "slave_id": slave,
                "available": agent["lease_state"] == "active",
                "lease_state": agent["lease_state"], "last_seen_at": agent.get("last_seen_at"),
                "base_actions": sorted(caps.get("base_actions", [])),
                "term_support": caps.get("term_support", []),
                "executor_descriptors": caps.get("executor_descriptors", []),
                "runtime_plugin_descriptors": caps.get("runtime_plugin_descriptors", []),
            }, [builtin["run-package"], builtin["activate-package"]], slave)

        bound: dict[str, list[tuple[Any, Any, list]]] = {}
        packages = await self.repo.list_capability_packages(workspace_id=self.settings.workspace_id)
        for package in packages:
            if (package.publication_state != "published"
                    or package.workspace_id != self.settings.workspace_id):
                continue
            package_uri = self.package_uri(package)
            exports = []
            declarations = []
            for export in package.action_exports:
                action = self.export_uri(package, export)
                contract = await self._metadata(export.io_contract_ref)
                argument_schema = OBJECT
                if contract.get("input_schema_ref"):
                    argument_schema = await self._metadata(ResourceRef.model_validate(contract["input_schema_ref"]))
                schema_uri = n.name("schemas", "calls", package.package_id, package.package_version,
                                    resource_label(export.action_descriptor_ref.resource_id), "sha256", export.action_descriptor_ref.digest)
                schema = CallInput.model_json_schema()
                schema["properties"]["arguments"] = argument_schema
                schemas[schema_uri] = schema
                offer = self.offer(action, schema_uri, n.schema("run-output"))
                exports.append(offer)
                descriptor = await self._metadata(export.action_descriptor_ref, descriptor=True)
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
                }
                declarations.append(declaration)
                if descriptor.get("resource_id"):
                    identity = descriptor["resource_id"]
                    if identity not in entries:
                        revisions = {"descriptors": []}
                        add(identity, "grip/action", revisions, [builtin["read"]], revisions)
                    ref = export.action_descriptor_ref.model_dump(mode="json", exclude_none=True)
                    if ref not in entries[identity].value["descriptors"]:
                        entries[identity].value["descriptors"].append(ref)
                add(action, "grip/action", declaration, [builtin["read"]], declaration)
            add(package_uri, "capability/package", {
                "package_id": package.package_id, "package_version": package.package_version,
                "digest": package.package_digest, "execution": package.execution.model_dump(mode="json"),
                "executable": package.is_executable, "actions": declarations,
            }, [builtin["read"]], package)
            for activation in await self.repo.list_package_activations(package):
                activation_uri = self.activation_uri(package, activation)
                target = (activation.target_resource_ref.resource_id
                          if activation.target_resource_ref else None)
                ready = activation.activation_state == "ready" and activation.desired_state == "running"
                unavailable_reason = (None if ready else
                                      "desired_state_" + activation.desired_state if activation.desired_state != "running" else
                                      "activation_" + activation.activation_state)
                metadata = {"package": package_uri, "host": n.name("slaves", activation.target_slave),
                            "name": package.package_id + ":" + activation.instance_name,
                            "description": f"Activated {package.package_id} {package.package_version} on {activation.target_slave}.",
                            "instance_name": activation.instance_name, "state": activation.activation_state,
                            "desired_state": activation.desired_state, "revision": activation.activation_revision,
                            "target": target,
                            "actions": [{**a, "available": ready, "unavailable_reason": unavailable_reason}
                                        for a in declarations]}
                authentication = (package.body or {}).get("authentication")
                add(activation_uri, "capability/activation", metadata,
                    [builtin["stop"], *([builtin["auth-start"]] if authentication and activation.desired_state == "running" else []), *(exports if ready else [])], (package, activation))
                if target and ready:
                    bound.setdefault(target, []).append((package, activation, exports))

        package_names = {p.version_ref: self.package_uri(p) for p in packages if p.workspace_id == self.settings.workspace_id}
        for resource in await self.repo.list_resources(self.settings.workspace_id):
            target = resource.resource_ref.resource_id
            candidates = bound.get(target, [])
            # Ambiguous instances are named explicitly, never chosen by order.
            offers = candidates[0][2] if len(candidates) == 1 else []
            add(target, definitions[resource.mcue_ref]["mcue"], {
                "mcue_ref": resource.mcue_ref,
                "attributes": resource.attributes,
                "attributes_revision": resource.attributes_revision,
                "state": resource.state,
                "package": package_names.get(resource.package_ref.resource_id) if resource.package_ref else None,
                "activations": [self.activation_uri(p, a) for p, a, _ in candidates],
                "managed_by": resource.managed_by_ref.resource_id if resource.managed_by_ref else None,
            }, [builtin["update-attributes"], *offers], candidates[0][:2] if len(candidates) == 1 else None)
        for record in await self.repo._all_records():
            if record.closure_contract is None or record.closure_contract.workspace_id != self.settings.workspace_id:
                continue
            add(n.name("runs", record.run_id), "execution/run", {"state": record.state},
                [builtin["read"], builtin["cancel"]], record)
        for name, offer in builtin.items():
            add(n.action(name), "grip/action", {"contract": offer["action"]}, [builtin["read"]], offer["action"])
        for target, schema in schemas.items():
            add(target, "grip/schema", {"language": "https://json-schema.org/draft/2020-12/schema"},
                [builtin["read"]], schema)
        for entry in entries.values():
            entry.description["revision"] = digest_json(entry.description, domain="loom/grip-description/v1")
        return entries

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
            "offers": [self.offer(self.names.action("read"), self.names.schema("read-input"), self.names.schema("read-output"))],
        }, {"content": value, "media_type": stat.media_type, "encoding": encoding})
        catalog[target].description["revision"] = digest_json(catalog[target].description, domain="loom/grip-description/v1")

    async def _package(self, target, catalog):
        entry = catalog.get(target)
        if entry is None or not isinstance(entry.value, CapabilityPackageVersion):
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

    async def invoke(self, request, catalog, http=None):
        entry = catalog.get(request.target)
        if entry is None:
            raise GripError("not_found")
        offer = next((o for o in entry.description["offers"] if o["action"]["uri"] == request.action), None)
        if offer is None:
            raise GripError("unsupported_action")
        schema = catalog[offer["action"]["input_schema"]].value
        if not Draft202012Validator(schema).is_valid(request.input):
            raise GripError("invalid_input")
        n = self.names
        data = request.input
        if request.action == n.action("list-resources"):
            return {"resources": [e.description["resource"] for e in catalog.values()
                                  if not data.get("mcue") or e.description["resource"]["mcue"] == data["mcue"]]}
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
                package_entry = catalog.get(payload.package)
                if package_entry is None or not isinstance(package_entry.value, CapabilityPackageVersion):
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
            if isinstance(entry.value, RunRecord):
                return {"state": entry.value.state, "outcome": entry.value.outcome}
            if isinstance(entry.value, CapabilityPackageVersion):
                return {**entry.value.model_dump(mode="json"), "resource": entry.value.version_ref}
            return entry.value
        if request.action == n.action("cancel"):
            record = await self.repo.cancel_run(entry.value.run_id, "grip_cancel")
            return {"state": record.state}
        if request.action == n.action("run-package"):
            payload = RunInput.model_validate(data)
            package = await self._package(payload.package, catalog)
            export = self._export(package, payload.action)
            return await self._submit(target=request.target, slave=entry.value, package=package,
                                      export=export, arguments=payload.arguments, key=payload.idempotency_key)
        if request.action == n.action("activate-package"):
            payload = ActivateInput.model_validate(data)
            package = await self._package(payload.package, catalog)
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
                                 credential_profile=payload.credential_profile)
            activations = await self.repo.list_package_activations(package)
            activation = next(a for a in activations if a.target_slave == entry.value and a.instance_name == payload.instance_name)
            return {"resource": self.activation_uri(package, activation), "state": activation.activation_state,
                    "revision": activation.activation_revision}
        package, activation = entry.value
        stop_retry = (request.action == n.action("stop") and activation.desired_state == "stopped"
                      and activation.last_idempotency_key == data["idempotency_key"]
                      and data["expected_revision"] == activation.activation_revision - 1)
        if data["expected_revision"] != activation.activation_revision and not stop_retry:
            raise GripError("invalid_input")
        if request.action == n.action("auth-start"):
            authentication = (package.body or {}).get("authentication") or {}
            if authentication.get("browser_path"):
                return await self.app.state.browser_auth.start(http, package, [activation.target_slave], activation.instance_name, expected_revision=data["expected_revision"])
            reports = await execute_auth_targets(
                self.repo, package, [activation.target_slave], workspace_id=self.settings.workspace_id,
                workers=self.app.state.workers, slaves=self.app.state.slaves,
                approved_digest=package.package_digest, instance_name=activation.instance_name,
                slave_gateway=None if self.app.state.embedded else self.app.state.slave_gateway,
                activation_revision=activation.activation_revision,
            )
            return {"health_reports": reports}
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

    async def _activate(self, package, slave, state, key, instance, target, config, *, credential_profile=None, expected_revision=None):
        reports = await execute_activation_targets(
            self.repo, package, [slave], desired_state=state, workspace_id=self.settings.workspace_id,
            workers=self.app.state.workers, slaves=self.app.state.slaves, idempotency_key=key,
            instance_name=instance, target_resource_ref=target, instance_config=config, expected_revision=expected_revision,
            credential_profile=credential_profile,
            slave_gateway=None if self.app.state.embedded else self.app.state.slave_gateway,
        )
        if any("error" in report for report in reports):
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
            if not self.settings.grip_token or not hmac.compare_digest(request.auth.credential.encode(), self.settings.grip_token.encode()):
                raise GripError("authentication_failed")
            if request.auth.tenant is not None and request.auth.tenant != self.names.root:
                raise GripError("forbidden")
            if self.protocol not in request.accepted_protocols:
                raise GripError("unsupported_binding")
            if request.deadline and datetime.fromisoformat(request.deadline.replace("Z", "+00:00")) <= datetime.now(timezone.utc):
                raise GripError("deadline_exceeded")
            catalog = await self.catalog()
            if request.target not in catalog and content_digest(request.target):
                await self._add_content(request.target, catalog)
            if request.target not in catalog:
                raise GripError("not_found")
            if request.deadline and datetime.fromisoformat(request.deadline.replace("Z", "+00:00")) <= datetime.now(timezone.utc):
                raise GripError("deadline_exceeded")
            if request.method == "DESCRIBE":
                body = catalog[request.target].description
                Description.model_validate(body)
            else:
                body = await self.invoke(request, catalog, http)
            return JSONResponse({"version": "grip.v1", "request_id": request_id, "kind": "reply", "body": body},
                                headers={"Cache-Control": "private, no-cache"})
        except GripError as exc:
            code = exc.code
        except ValidationError:
            code = "invalid_message"
        except ValueError as exc:
            code = str(exc) if str(exc) in {
                "attributes_revision_conflict", "mcue_version_conflict", "resource_mcue_conflict",
                "resource_attributes_require_update", "mcue_attributes_invalid",
                "resource_mcue_migration_required",
            } else "invalid_input" if request_id else "invalid_message"
        except (TypeError, UnicodeError):
            code = "invalid_input" if request_id else "invalid_message"
        except (KeyError, FileNotFoundError):
            code = "not_found"
        except DomainError:
            code = "invalid_input"
        except Exception:
            # Never include credentials, runtime details or exception payloads.
            log.error("GRIP action failed (request_id=%s)", request_id)
            code = "internal_error"
        if request_id is None:
            return JSONResponse(status_code=400, content={"error": code})
        return JSONResponse({"version": "grip.v1", "request_id": request_id, "kind": "error", "error_code": code})
