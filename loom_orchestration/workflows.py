"""Public, evidence-preserving workflows for external agents.

These helpers use the same public contracts as LoomClient. They neither infer
resource identities nor provision existing instances as a side effect of reading.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
import sqlite3
import time

from jsonschema import Draft202012Validator
import rfc8785

from .contracts import ResourceRef, TaskClosure
from .package import OrchestratorPackage
from .submission import SubmissionBuilder


def _wire(ref):
    return ref.model_dump(mode="json")


def _validate(schema, arguments):
    errors = list(Draft202012Validator(schema).iter_errors(arguments))
    if errors:
        # Report the contract location, never echo business data or credentials.
        locations = sorted({"/" + "/".join(map(str, e.absolute_schema_path)) for e in errors})
        raise ValueError("action_arguments_invalid:" + ",".join(locations))


async def inventory(client, *, include_stopped=False):
    """Compact all business resources and instances without losing categories."""
    rows = await client.resources()
    result = {"resources": [], "activations": [], "slaves": []}
    for row in rows:
        m = row["metadata"]
        base = {"uri": row["uri"], "mcue": row["mcue"], "mcue_ref": m.get("mcue_ref"),
                "dependencies": m.get("dependencies")}
        if row["mcue"] == "capability/activation":
            if not include_stopped and m.get("state") == "stopped":
                continue
            result["activations"].append({**base, **{k: m.get(k) for k in (
                "package", "host", "target", "instance_name", "state", "available", "authentication")},
                "actions": [{k: a.get(k) for k in ("name", "description", "interaction", "available")}
                            for a in m.get("actions", [])]})
        elif row["mcue"] == "computing/slave":
            result["slaves"].append({**base, "available": m.get("available"), "attributes": m.get("attributes", {})})
        elif row["mcue"] not in {"capability/package", "execution/run"} and not row["mcue"].startswith("grip/"):
            result["resources"].append({**base, **{k: m.get(k) for k in (
                "attributes", "state", "activations", "available")}})
    return result


async def action_contract(client, target, action_name):
    description = await client.describe(target, refresh=True)
    metadata = description["resource"]["metadata"]
    actions = [a for a in metadata.get("actions", []) if a.get("name") == action_name or a["uri"] == action_name]
    if len(actions) != 1:
        raise ValueError("action_not_found_or_ambiguous")
    action = actions[0]
    schemas = {}
    for field in ("arguments_schema", "result_schema"):
        if not action.get(field):
            raise ValueError("action_contract_incomplete")
        schemas[field] = await client.read_json(ResourceRef.model_validate(action[field]))
    return {"resource": description["resource"], "action": action, **schemas}


async def read_package_source(client, package_uri):
    """Read publisher-selected source for the exact immutable package, never run it.

    A matching image reference is a publisher assertion, not a build attestation.
    No host paths, image extraction, repository cloning or edits are performed.
    """
    package = await client.invoke(package_uri, client.names.action("read"), {})
    if package["execution"] == {"kind": "process:json_stdio", "version": "1"}:
        ref = ResourceRef.model_validate(package["body"]["program_content_ref"])
        return {"package": package_uri, "source_ref": _wire(ref), "source": await client.read_json(ref)}
    refs = []
    for export in package["action_exports"]:
        descriptor = await client.descriptor(ResourceRef.model_validate(export["action_descriptor_ref"]))
        diagnostic = descriptor.get("diagnostics") or {}
        if diagnostic.get("source_bundle_ref"):
            if diagnostic.get("image_ref") != package["body"].get("image_ref"):
                raise ValueError("diagnostic_source_image_mismatch")
            refs.append(diagnostic["source_bundle_ref"])
    if not refs:
        raise ValueError("package_source_not_published")
    if any(ref != refs[0] for ref in refs):
        raise ValueError("diagnostic_source_ambiguous")
    ref = ResourceRef.model_validate(refs[0])
    bundle = await client.read_json(ref)
    from .naming import content_digest
    if hashlib.sha256(rfc8785.dumps(bundle)).hexdigest() != content_digest(ref.resource_id):
        raise ValueError("diagnostic_source_digest_mismatch")
    if bundle.get("format") != "loom.source-bundle.v1" or bundle.get("image_ref") != package["body"].get("image_ref"):
        raise ValueError("diagnostic_source_image_mismatch")
    return {"package": package_uri, "source_ref": _wire(ref), **bundle}


class CallLedger:
    """Durable pre-call budgets for callers using execute_action.

    This is a client-side guard, not a security boundary against bypassing the
    SDK. Failed and uncertain dispatched calls consume budget. Reusing an
    operation key resumes its recorded receipt rather than charging it again.
    Character limits apply to every string in the business `texts` input.
    """
    def __init__(self, path, *, max_calls, max_text_characters=None, max_text_length=None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.limits = [max_calls, max_text_characters, max_text_length]
        if any(v is not None and (isinstance(v, bool) or not isinstance(v, int) or v < 0) for v in self.limits):
            raise ValueError("call_budget_invalid")
        with sqlite3.connect(self.path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS policy (id INTEGER PRIMARY KEY, value TEXT)")
            db.execute("CREATE TABLE IF NOT EXISTS calls (key TEXT PRIMARY KEY, signature TEXT, characters INTEGER, receipt TEXT)")
            db.execute("INSERT OR IGNORE INTO policy VALUES (1, ?)", (json.dumps(self.limits),))
            if json.loads(db.execute("SELECT value FROM policy WHERE id=1").fetchone()[0]) != self.limits:
                raise ValueError("call_budget_changed")
        self.path.chmod(0o600)

    def reserve(self, target, action, arguments, key):
        texts = arguments.get("texts", [])
        if not isinstance(texts, list) or any(not isinstance(t, str) for t in texts):
            raise ValueError("text_budget_input_invalid")
        if self.limits[2] is not None and any(len(t) > self.limits[2] for t in texts):
            raise ValueError("per_text_budget_exceeded")
        characters = sum(map(len, texts))
        identity = json.dumps([target, action, key])
        signature = hashlib.sha256(rfc8785.dumps(arguments)).hexdigest()
        with sqlite3.connect(self.path) as db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute("SELECT signature, receipt FROM calls WHERE key=?", (identity,)).fetchone()
            if prior:
                if prior[0] != signature:
                    raise ValueError("operation_key_reused_with_different_arguments")
                return identity, False, json.loads(prior[1]) if prior[1] else None
            count, used = db.execute("SELECT COUNT(*), COALESCE(SUM(characters), 0) FROM calls").fetchone()
            if count >= self.limits[0] or (self.limits[1] is not None and used + characters > self.limits[1]):
                raise ValueError("call_budget_exceeded")
            db.execute("INSERT INTO calls VALUES (?, ?, ?, NULL)", (identity, signature, characters))
        return identity, True, None

    def record(self, identity, receipt):
        with sqlite3.connect(self.path) as db:
            db.execute("UPDATE calls SET receipt=? WHERE key=?", (json.dumps(receipt), identity))

    def summary(self):
        with sqlite3.connect(self.path) as db:
            count, chars = db.execute("SELECT COUNT(*), COALESCE(SUM(characters), 0) FROM calls").fetchone()
            rows = db.execute("SELECT key, receipt FROM calls ORDER BY rowid").fetchall()
        return {"calls": count, "text_characters": chars,
                "operations": [{"identity": json.loads(k), "receipt": json.loads(r) if r else None} for k, r in rows]}


async def wait_run(client, run, *, timeout=None, interval=1):
    """Wait for the Run's terminal state and verify its persisted JSON.

    ``timeout`` is an optional local polling watchdog. It is not the Run's
    execution budget; by default polling follows the server-owned Run deadline
    until a terminal state is returned.
    """
    if (timeout is not None and timeout <= 0) or interval <= 0:
        raise ValueError("poll_budget_invalid")
    run_id = run.rsplit("/", 1)[-1]
    end = time.monotonic() + timeout if timeout is not None else None
    while True:
        state = await client.run(run_id)
        if state["state"] in {"completed", "failed", "cancelled", "interrupted", "awaiting_auth", "awaiting_decision"}:
            break
        if end is not None and time.monotonic() >= end:
            return {"status": "poll_timeout", "run": state}
        delay = interval if end is None else min(interval, max(0, end - time.monotonic()))
        await asyncio.sleep(delay)
    result = {"status": state["state"], "run": state}
    if state["state"] == "completed":
        ref = ResourceRef.model_validate(state["outcome"]["resource_ref"])
        value = await client.read_json(ref)
        from .naming import content_digest
        if hashlib.sha256(rfc8785.dumps(value)).hexdigest() != content_digest(ref.resource_id):
            raise ValueError("result_content_digest_mismatch")
        result.update(value=value, resource_ref=_wire(ref), digest_verified=True)
    return result


async def execute_action(client, target, action_name, arguments, *, idempotency_key, ledger=None, timeout=None):
    """Validate business input, invoke the exact instance, and resolve task output."""
    contract = await action_contract(client, target, action_name)
    action = contract["action"]
    _validate(contract["arguments_schema"], arguments)
    interaction = action.get("interaction")
    if interaction not in {"task", "request-reply"}:
        raise ValueError("action_interaction_unsupported")
    if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 128:
        raise ValueError("operation_key_required")
    receipt = None
    if ledger:
        identity, fresh, receipt = ledger.reserve(target, action["uri"], arguments, idempotency_key)
        if not fresh and receipt is None and interaction == "request-reply":
            raise ValueError("prior_direct_call_outcome_unknown")
    if receipt is None:
        payload = arguments
        if interaction == "task":
            payload = {"arguments": arguments, "expected_revision": contract["resource"]["metadata"]["revision"],
                       "idempotency_key": idempotency_key}
        receipt = await client.invoke(target, action["uri"], payload)
        if ledger:
            ledger.record(identity, receipt)
    result = {"target": target, "action": action["uri"], "interaction": interaction, "receipt": receipt}
    if interaction == "task":
        result.update(await wait_run(client, receipt["run"], timeout=timeout))
    else:
        result.update(status="completed", value=receipt)
    return result


async def publish_function(client, *, package_id, package_version, action_name, source, input_schema, output_schema):
    """Publish a single JSON-stdio function; no execution or activation."""
    compile(source, '<function>', 'exec')
    for schema in (input_schema, output_schema):
        Draft202012Validator.check_schema(schema)
    program = await client.upload_content(source, media_type="text/x-python")
    inputs = await client.upload_content(input_schema, media_type="application/schema+json")
    outputs = await client.upload_content(output_schema, media_type="application/schema+json")
    io = await client.upload_content({"schema_version": "io.v1", "input_schema_ref": _wire(inputs),
                                     "output_schema_ref": _wire(outputs)}, media_type="application/vnd.loom.io-contract+json")
    descriptor = await client.upload_content({"schema_version": "action.v1", "resource_id": client.names.name("actions", package_id, action_name),
                                            "name": action_name}, media_type="application/vnd.loom.action-descriptor+json")
    return await client.register_package({
        "package_id": package_id, "package_version": package_version, "package_type": "function",
        "execution": {"kind": "process:json_stdio", "version": "1"},
        "action_exports": [{"action_descriptor_ref": _wire(descriptor), "io_contract_ref": _wire(io),
                            "effect_class": "Sandboxed", "permissions": [], "replay_safety": "DeclaredByPackage", "runtime_binding": {}}],
        "body": {"program_content_ref": _wire(program)},
    })


MAP_REDUCE_SOURCE = '''async def orchestrate(ctx: "OrchestrationContext", input_ref: "ResourceRef") -> "ResourceRef":
    data = await ctx.read_json(input_ref)
    handles = []
    for i, job in enumerate(data["jobs"]):
        handles.append(ctx.emit_node("map-" + str(i), data["worker"]["package"],
            data["worker"]["descriptor"], [job["input"]], activation_ref=job["activation"]))
    results = []
    for handle in handles:
        results.append(await ctx.result(handle))
    merged = ctx.emit_node("reduce", data["reducer"]["package"],
        data["reducer"]["descriptor"], results, activation_ref=data["reducer"]["activation"])
    return await ctx.result(merged)
'''


async def submit_map_reduce(client, *, worker_package, worker_action, reducer_package, reducer_action,
                            jobs, reducer_target, package_id, request_id, goal,
                            max_live_nodes=2, execution_timeout_seconds=600):
    """One Run with >=2 map nodes and an actual remote final reduction node.

    jobs: [{"target": discovered Slave URI, "arguments": business JSON}, ...].
    Publish distinct worker/reducer schemas. The reducer consumes {inputs: [...]}.
    This creates/reuses default function activations for these user-owned packages
    on the selected Slaves; target_resource_ref denotes a managed business
    resource and must not be misused as a Slave placement hint.
    """
    if len(jobs) < 2 or max_live_nodes < 1 or max_live_nodes > len(jobs):
        raise ValueError("map_reduce_budget_invalid")
    worker = await action_contract(client, worker_package, worker_action)
    reducer = await action_contract(client, reducer_package, reducer_action)
    for c in (worker, reducer):
        if c["resource"]["metadata"]["execution"] != {"kind": "process:json_stdio", "version": "1"}:
            raise ValueError("json_stdio_function_required")
    for job in jobs:
        _validate(worker["arguments_schema"], job["arguments"])
    for uri in {reducer_target, *(j["target"] for j in jobs)}:
        slave = (await client.describe(uri, refresh=True))["resource"]
        if slave["mcue"] != "computing/slave" or not slave["metadata"].get("available"):
            raise ValueError("available_slave_required")
    def refs(c):
        m = c["resource"]["metadata"]
        return {"package": _wire(ResourceRef(resource_id=c["resource"]["uri"], version_or_digest=m["digest"])),
                "descriptor": c["action"]["action_descriptor_ref"]}
    activations = []
    async def activate(package, slave, label):
        checked = await client.invoke(slave, client.names.action("check-package"), {"package": package, "instance_name": "default"})
        if not checked.get("ready"):
            raise ValueError("function_package_not_ready")
        receipt = await client.invoke(slave, client.names.action("activate-package"), {
            "package": package, "instance_name": "default", "idempotency_key": f"{request_id}-activate-{label}"})
        activations.append(receipt)
        return _wire(await client.activation_ref(receipt["resource"]))
    data = {"worker": refs(worker), "reducer": {**refs(reducer),
            "activation": await activate(reducer_package, reducer_target, "reduce")}, "jobs": []}
    for index, job in enumerate(jobs):
        ref = await client.upload_content(job["arguments"])
        data["jobs"].append({"activation": await activate(worker_package, job["target"], str(index)), "input": _wire(ref)})
    input_ref = await client.upload_content(data)
    program = await client.upload_content(MAP_REDUCE_SOURCE, media_type="text/x-python")
    inputs = await client.upload_content({"type": "object"}, media_type="application/schema+json")
    outputs = await client.upload_content(reducer["result_schema"], media_type="application/schema+json")
    io = await client.upload_content({"schema_version": "io.v1", "input_schema_ref": _wire(inputs), "output_schema_ref": _wire(outputs)},
                                     media_type="application/vnd.loom.io-contract+json")
    action_id = client.names.name("actions", package_id, "orchestrate")
    descriptor = await client.upload_content({"schema_version": "action.v1", "resource_id": action_id, "name": "orchestrate"},
                                             media_type="application/vnd.loom.action-descriptor+json")
    manifest = OrchestratorPackage(program_content_ref=program, action_descriptor_ref=descriptor, io_contract_ref=io,
        allowed_node_package_refs=[ResourceRef.model_validate(data[k]["package"]) for k in ("worker", "reducer")],
        max_nodes=len(jobs)+1, max_live_nodes=max_live_nodes, package_id=package_id).manifest()
    published = await client.register_package(manifest)
    published_description = await client.describe(published["resource"])
    exported_action, = published_description["resource"]["metadata"]["actions"]
    plan = SubmissionBuilder(goal=goal,
        body=TaskClosure(closure_id=request_id, program={"action_ref": exported_action["uri"], "io_contract_ref": _wire(io)}),
        orchestrator_package=ResourceRef(resource_id=published["resource"], version_or_digest=published["package_digest"]),
        input_binding=input_ref, workspace_id=client.workspace_id,
        resource_budget={"max_nodes": len(jobs)+1, "max_live_nodes": max_live_nodes,
                         "execution_timeout_seconds": execution_timeout_seconds, "cleanup_timeout_seconds": 60}).build(request_id=request_id)
    receipt = await client.submit(request_id=request_id, plan=plan)
    return {"receipt": receipt, "orchestrator": published, "activations": activations, "plan": plan.model_dump(mode="json")}
