from copy import deepcopy
import json
from pathlib import Path
import stat

import pytest
from pydantic import ValidationError
from loom_orchestration.grip import GripError

from loom_business_skills import Draft, Memory, Problem, Store
from loom_business_skills.model import brief
from conftest import PublicClient, draft


async def learned(memory, client, **kwargs):
    p = draft(client, **kwargs)
    await memory.learn(p)
    return p


async def test_documented_executed_and_verified_are_distinct(memory, client, store):
    await learned(memory, client)
    assert store.get("paper-review")["evidence"]["catalog"]["level"] == "documented"
    execution, result = await memory.run("paper-review", "catalog", {"value": 21}, idempotency_key="task-a")
    assert result["value"]["value"] == 42
    assert store.get("paper-review")["evidence"]["catalog"]["level"] == "executed"
    await memory.accept("paper-review", "catalog", execution["id"])
    record = store.get("paper-review")
    assert record["evidence"]["catalog"]["level"] == "verified"
    text = (store.root / "experiences/paper-review/SKILL.md").read_text()
    assert "(verified)" in text
    for path in store.root.rglob("*"):
        if path.is_file():
            data = path.read_bytes()
            assert client.token.encode() not in data
            assert b"RAW-BUSINESS-RESPONSE-DO-NOT-CACHE" not in data


async def test_prepare_reads_fresh_dynamic_descriptions_but_not_immutable_bodies(memory, client):
    await learned(memory, client)
    cold = len(client.calls)
    client.calls.clear()
    response = await memory.prepare("paper-review")
    assert response["status"] == "ready"
    assert len(client.calls) < cold
    assert all(call[0] == "describe" and call[2] is True for call in client.calls)
    assert len(json.dumps(response)) < 8000


@pytest.mark.parametrize("mutation", ["package", "permission", "schema", "action", "interaction", "revision", "host", "identity", "binding"])
async def test_contract_drift_blocks_execution(memory, client, mutation):
    p = await learned(memory, client)
    d = client.descriptions[p["bindings"]["catalog"]["target"]]
    m = d["resource"]["metadata"]
    a = m["actions"][0]
    if mutation == "package":
        client.descriptions[m["package"]]["resource"]["metadata"]["digest"] = "new-digest"
    elif mutation == "permission":
        a["permissions"] = ["ExternalWrite"]
    elif mutation == "schema":
        a["arguments_schema"]["version_or_digest"] = "changed"
    elif mutation == "action":
        a["uri"] += "-new"
    elif mutation == "interaction":
        a["interaction"] = "task"
    elif mutation == "revision":
        m["revision"] += 1
    elif mutation == "host":
        m["host"] = client.names.name("slaves", "missing")
    elif mutation == "identity":
        m["instance_name"] = "replacement"
    else:
        d["offers"][0]["bindings"][0]["endpoint"] = "https://replacement.example/.well-known/grip"
    result = await memory.prepare("paper-review")
    assert result["status"] in {"stale", "blocked"}
    assert "experience" not in result
    with pytest.raises(Problem, match="execution_blocked"):
        await memory.run("paper-review", "catalog", {}, idempotency_key="blocked")
    assert not client.dispatches


@pytest.mark.parametrize("failure", ["auth", "offline", "missing", "permission"])
async def test_dynamic_failure_is_retryable_not_permanent(memory, client, failure):
    p = await learned(memory, client)
    target = p["bindings"]["catalog"]["target"]
    if failure == "auth":
        client.descriptions[target]["resource"]["metadata"]["authentication"]["status"] = "auth_required"
    else:
        client.errors[target] = {"offline": OSError("secret network detail"), "missing": GripError("not_found"),
                                 "permission": GripError("permission_denied")}[failure]
    report = await memory.prepare("paper-review")
    assert report["status"] == ("offline" if failure == "offline" else "blocked")
    assert "secret network detail" not in json.dumps(report)
    assert "experience" not in report
    client.errors.clear()
    client.descriptions[target]["resource"]["metadata"]["authentication"]["status"] = "authenticated"
    assert (await memory.prepare("paper-review"))["status"] == "ready"


async def test_all_dependencies_checked_before_any_step(memory, client):
    p = await learned(memory, client, bindings={"catalog": client.add("catalog"), "metrics": client.add("metrics")})
    target = p["bindings"]["metrics"]["target"]
    client.descriptions[target]["resource"]["metadata"]["available"] = False
    with pytest.raises(Problem, match="execution_blocked"):
        await memory.run(p["id"], "catalog", {}, idempotency_key="no-dispatch")
    assert not client.dispatches


async def test_declared_dependency_graph_cycles_and_partial_failure(memory, client):
    p = await learned(memory, client)
    target = p["bindings"]["catalog"]["target"]
    host = client.descriptions[target]["resource"]["metadata"]["host"]
    host_md = client.descriptions[host]["resource"]["metadata"]
    host_md["dependencies"] = [{"path": "parent", "resource": target}]
    assert (await memory.check(p["id"]))["status"] == "stale"
    await memory.refresh(p["id"])
    assert (await memory.check(p["id"]))["status"] == "ready"
    host_md["available"] = False
    assert (await memory.check(p["id"]))["status"] == "blocked"


async def test_explicit_extra_dependency_is_checked(memory, client):
    b = client.add("catalog")
    extra = client.add("metrics")["target"]
    b["dependencies"] = [extra]
    await learned(memory, client, bindings={"catalog": b})
    client.errors[extra] = GripError("not_found")
    assert (await memory.check("paper-review"))["status"] == "blocked"


@pytest.mark.parametrize("different", ["workspace", "observer"])
async def test_cross_environment_never_uses_historical_uri(memory, client, store, different):
    await learned(memory, client)
    other = PublicClient(workspace="elsewhere") if different == "workspace" else PublicClient(base_url="https://other.example")
    new = Memory(store, other)
    assert (await new.prepare("paper-review"))["status"] == "needs_rebind"
    assert not other.calls
    with pytest.raises(Problem, match="explicit_rebind_required"):
        await new.refresh("paper-review")
    record = await new.rebind("paper-review", "paper-other", {"catalog": other.add("catalog")})
    assert record["evidence"]["catalog"]["level"] == "documented"
    assert (await new.prepare("paper-other"))["status"] == "ready"


async def test_ambiguous_resource_requires_explicit_named_activation(memory, client):
    p = draft(client)
    target = client.names.name("resources", "business")
    client.descriptions[target] = {"resource": {"uri": target, "mcue": "computing/vm", "metadata": {
        "activations": [client.add("catalog")["target"], client.add("metrics")["target"]]}}, "offers": []}
    p["bindings"]["catalog"]["target"] = target
    with pytest.raises(Problem) as error:
        await memory.learn(p)
    detail = error.value.details["issue"]
    assert detail["code"] == "named_activation_required" and detail["candidate_count"] == 2
    assert not client.dispatches


async def test_refresh_preserves_only_unchanged_verified_steps(memory, client, store):
    p = await learned(memory, client, bindings={"catalog": client.add("catalog"), "metrics": client.add("metrics")})
    for step in ["catalog", "metrics"]:
        e, _ = await memory.run(p["id"], step, {}, idempotency_key=step)
        await memory.accept(p["id"], step, e["id"])
    target = p["bindings"]["catalog"]["target"]
    client.descriptions[target]["resource"]["metadata"]["revision"] += 1
    await memory.refresh(p["id"])
    evidence = store.get(p["id"])["evidence"]
    assert evidence["catalog"]["level"] == "documented"
    assert "last_execution" not in evidence["catalog"]
    assert evidence["metrics"]["level"] == "verified"
    p["steps"][1]["acceptance"] = "Use the newly clarified business condition."
    await memory.refresh(p["id"], p)
    assert store.get(p["id"])["evidence"]["metrics"]["level"] == "documented"


async def test_accept_rejects_invented_execution_and_drift(memory, client):
    p = await learned(memory, client)
    with pytest.raises(Problem, match="verified_execution_required"):
        await memory.accept(p["id"], "catalog", "invented")
    execution, _ = await memory.run(p["id"], "catalog", {}, idempotency_key="real")
    target = p["bindings"]["catalog"]["target"]
    client.descriptions[target]["resource"]["metadata"]["revision"] += 1
    with pytest.raises(Problem, match="acceptance_blocked"):
        await memory.accept(p["id"], "catalog", execution["id"])


@pytest.mark.parametrize("secret", ["Bearer abcdefghijklmnopqrstuvwxyz", "api_key=abcdefghijklmnopqrstuv", "-----BEGIN PRIVATE KEY-----", "private-session-token-should-never-be-saved"])
async def test_credentials_rejected_before_learning(memory, client, store, secret):
    p = draft(client)
    p["steps"][0]["instruction"] = secret
    with pytest.raises(Problem, match="credential_in_experience"):
        await memory.learn(p)
    assert not client.calls
    assert store.find("") == []


async def test_separate_cache_instances_remove_and_permissions(memory, client, store):
    await learned(memory, client)
    other = Store(store.root)
    assert other.find("论文")[0]["id"] == "paper-review"
    assert stat.S_IMODE(store.root.stat().st_mode) == 0o700
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert stat.S_IMODE((store.root / "experiences/paper-review/SKILL.md").stat().st_mode) == 0o600
    stale = store.get("paper-review")
    other.put(other.get("paper-review"))
    with pytest.raises(Problem, match="experience_changed"):
        store.put(stale)
    other.remove("paper-review")
    assert not (store.root / "experiences/paper-review").exists()
    assert store.find("papers") == []
    await learned(memory, client)
    assert (await memory.prepare("paper-review"))["status"] == "ready"


async def test_thousand_experiences_do_not_expand_context_or_registered_skills(memory, client, store):
    await learned(memory, client)
    template = store.get("paper-review")
    for i in range(1000):
        record = deepcopy(template)
        record.pop("_revision")
        record["draft"]["id"] = f"paper-{i}"
        store.put(record)
    found = store.find("papers")
    assert len(found) == 3
    assert len(json.dumps(found)) < 1800
    assert all("steps" not in r and "contracts" not in r for r in found)
    assert len(store.find("papers", limit=5)) == 5
    with pytest.raises(Problem, match="search_budget_invalid"):
        store.find("papers", limit=6)
    plugin = Path(__file__).resolve().parents[1]
    assert len(list((plugin / "skills").glob("*/SKILL.md"))) == 1
    assert len(json.dumps(brief(template))) < 8000


def test_draft_context_budget_and_unexpected_data_rejected(client):
    p = draft(client)
    p["results"] = {"raw": "must not be stored"}
    with pytest.raises(ValidationError):
        Draft.model_validate(p)
    p.pop("results")
    p["steps"][0]["instruction"] = "x" * 601
    with pytest.raises(ValidationError):
        Draft.model_validate(p)


@pytest.mark.parametrize("key", ["../outside", "/tmp/outside", "", "a/b"])
def test_cache_ids_are_not_paths(store, key):
    with pytest.raises(Problem, match="invalid_experience_id"):
        store.remove(key)
