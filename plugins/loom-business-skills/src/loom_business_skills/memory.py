from __future__ import annotations

from copy import deepcopy
import time
from uuid import uuid4

from loom_orchestration import execute_action

from .live import Reader, issue
from .model import Draft, Problem, brief, environment, now, reject_secrets


class Memory:
    def __init__(self, store, client):
        self.store, self.client = store, client

    async def learn(self, proposal, *, previous=None):
        draft = Draft.model_validate(proposal).model_dump(mode="json")
        reject_secrets(draft, token=self.client.token)
        env = environment(self.client)
        if previous and (previous["draft"]["id"] != draft["id"] or previous["environment"] != env or previous["draft"]["bindings"] != draft["bindings"]):
            raise Problem("explicit_rebind_required")
        reader = Reader(self.client)
        started = time.monotonic()
        contracts = {}
        for name, binding in draft["bindings"].items():
            try:
                contracts[name] = await reader.binding(binding, bodies=True)
            except Exception as exc:
                raise Problem("learning_blocked", binding=name, issue=issue(exc)) from None
        timestamp = now()
        evidence = {}
        for step in draft["steps"]:
            level = {"level": "documented", "discovered_at": timestamp}
            if previous:
                old_step = next((s for s in previous["draft"]["steps"] if s["id"] == step["id"]), None)
                name = step["binding"]
                if old_step == step and previous["contracts"][name]["local_fingerprint"] == contracts[name]["local_fingerprint"]:
                    level = deepcopy(previous["evidence"][step["id"]])
            evidence[step["id"]] = level
        record = {"format": 1, "draft": draft, "environment": env, "contracts": contracts,
                  "created_at": previous["created_at"] if previous else timestamp, "learned_at": timestamp,
                  "evidence": evidence, "last_check": {"status": "ready", "at": timestamp, "issues": []},
                  "learning_metrics": {"sdk_operations": reader.calls, "elapsed_ms": round((time.monotonic() - started) * 1000)}}
        if previous:
            record["_revision"] = previous["_revision"]
        reject_secrets(record, token=self.client.token)
        return self.store.put(record)

    async def check(self, key):
        record = self.store.get(key)
        if record["environment"] != environment(self.client):
            return {"status": "needs_rebind", "issues": [{"code": "environment_mismatch"}], "sdk_operations": 0, "at": now()}
        reader, issues = Reader(self.client), []
        started = time.monotonic()
        for name, binding in record["draft"]["bindings"].items():
            try:
                current = await reader.binding(binding)
                if current["local_fingerprint"] != record["contracts"][name]["local_fingerprint"]:
                    issues.append({"binding": name, "code": "contract_changed"})
            except Exception as exc:
                issues.append({"binding": name, **issue(exc)})
        status = ("offline" if any(i["code"] == "offline" for i in issues) else
                  "stale" if any(i["code"] == "contract_changed" for i in issues) else
                  "blocked" if issues else "ready")
        result = {"status": status, "issues": issues, "at": now(), "sdk_operations": reader.calls,
                  "elapsed_ms": round((time.monotonic() - started) * 1000)}
        record["last_check"] = result
        self.store.put(record)
        return result

    async def prepare(self, key):
        report = await self.check(key)
        if report["status"] == "ready":
            report["experience"] = brief(self.store.get(key))
        return report

    async def refresh(self, key, proposal=None):
        record = self.store.get(key)
        return await self.learn(proposal or record["draft"], previous=record)

    async def rebind(self, key, new_id, bindings):
        draft = deepcopy(self.store.get(key)["draft"])
        if set(bindings) != set(draft["bindings"]):
            raise Problem("complete_binding_map_required")
        draft.update(id=new_id, bindings=bindings)
        return await self.learn(draft)

    async def run(self, key, step_id, arguments, *, idempotency_key, timeout=120):
        report = await self.check(key)
        if report["status"] != "ready":
            raise Problem("execution_blocked", check=report)
        record = self.store.get(key)
        step = next((s for s in record["draft"]["steps"] if s["id"] == step_id), None)
        if step is None:
            raise Problem("step_not_found")
        binding = record["draft"]["bindings"][step["binding"]]
        contract = record["contracts"][step["binding"]]
        if contract["contract"]["action"]["interaction"] not in {"task", "request-reply"}:
            raise Problem("stream_requires_public_sdk")
        execution = {"id": uuid4().hex, "at": now(), "local_fingerprint": contract["local_fingerprint"]}
        try:
            result = await execute_action(self.client, binding["target"], binding["action"], arguments,
                                          idempotency_key=idempotency_key, timeout=timeout)
            execution["status"] = result["status"]
            if result.get("resource_ref"):
                execution["resource_ref"] = result["resource_ref"]
            receipt = result.get("receipt")
            if result.get("interaction") == "task" and isinstance(receipt, dict):
                execution["run"] = receipt.get("run")
        except Exception as exc:
            execution.update(status="failed_or_unknown", issue=issue(exc))
            record["evidence"][step_id]["last_execution"] = execution
            self.store.put(record)
            raise Problem("step_call_failed", execution=execution) from None
        evidence = record["evidence"][step_id]
        evidence["last_execution"] = execution
        # A fresh execution is not an acceptance decision, even for a previously verified step.
        if execution["status"] == "completed":
            evidence["level"] = "executed"
        reject_secrets(record, token=self.client.token)
        self.store.put(record)
        return execution, result

    async def accept(self, key, step_id, execution_id):
        report = await self.check(key)
        if report["status"] != "ready":
            raise Problem("acceptance_blocked", check=report)
        record = self.store.get(key)
        evidence = record["evidence"].get(step_id, {})
        execution = evidence.get("last_execution", {})
        step = next((s for s in record["draft"]["steps"] if s["id"] == step_id), None)
        if (step is None or execution.get("id") != execution_id or execution.get("status") != "completed"
                or execution.get("local_fingerprint") != record["contracts"][step["binding"]]["local_fingerprint"]):
            raise Problem("verified_execution_required")
        evidence.update(level="verified", verified_at=now(), verified_execution=execution_id)
        return self.store.put(record)
