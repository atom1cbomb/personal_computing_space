#!/usr/bin/env python3
"""Live band-gap acceptance using GRIP discovery and complete-plan submission.

Uses public SDK APIs. Optional operator-owned Docker failure injection remains
explicit; this harness does not submit prompts to a server-side agent.
"""
from __future__ import annotations

from loom_orchestration.naming import content_digest

import argparse
import asyncio
import json
import math
import os
import random
import subprocess
import time
from typing import Any, Callable
from uuid import uuid4

from loom_orchestration import LoomClient, OrchestratorPackage, ResourceRef, SubmissionBuilder, TaskClosure

FAMILIES = ["perovskite", "rutile", "zincblende", "wurtzite", "rocksalt"]


class SlaveFailureController:
    def __init__(
        self,
        *,
        docker_bin: str,
        compose_file: str,
        run_command: Callable[[list[str]], Any] | None = None,
    ) -> None:
        self.docker_bin = docker_bin
        self.compose_file = compose_file
        self.run_command = run_command or self._run_command
        self.injected = False
        self.stopped = False
        self.execution_id: str | None = None
        self.execution_epoch: int | None = None

    @staticmethod
    def _run_command(command: list[str]) -> None:
        subprocess.run(command, check=True)

    def observe(self, run: dict[str, Any]) -> None:
        if self.execution_id is None and run.get("execution_id"):
            self.execution_id = str(run["execution_id"])
            self.execution_epoch = int(run.get("execution_epoch", 1))
        if self.injected:
            return
        completed_node_ids = {
            str(node.get("node_id"))
            for node in run.get("dynamic_nodes") or []
            if node.get("state") == "completed"
        }
        active_slave_a = any(
            attempt.get("target") == "slave-a"
            and attempt.get("state") in {"created", "running"}
            and str(attempt.get("node_id")) not in completed_node_ids
            for attempt in run.get("attempts") or []
        )
        if not completed_node_ids or not active_slave_a:
            return
        self.run_command(
            [self.docker_bin, "compose", "-f", self.compose_file, "stop", "slave-a"]
        )
        self.injected = True
        self.stopped = True

    def restore(self) -> None:
        if not self.stopped:
            return
        self.run_command(
            [self.docker_bin, "compose", "-f", self.compose_file, "start", "slave-a"]
        )
        self.stopped = False


def make_corpus(seed: int, n: int) -> list[dict]:
    rng = random.Random(seed)
    items = []
    for i in range(n):
        family = FAMILIES[i % len(FAMILIES)]
        base = 1.0 + (i // len(FAMILIES)) * 0.3
        e_gap = round(base + rng.uniform(-0.4, 0.4), 3)
        items.append(
            {
                "sample": f"sample-{i:04d}",
                "family": family,
                "e_gap": e_gap,
                "functional": "PBE",
                "kpoints": 8,
            }
        )
    return items


def _stats(values: list[float]) -> dict:
    n = len(values)
    s = sum(values)
    sq = sum(x * x for x in values)
    mean = s / n
    var = max(0.0, sq / n - mean * mean)
    return {
        "count": n,
        "sum": round(s, 6),
        "min": round(min(values), 6),
        "max": round(max(values), 6),
        "mean": round(mean, 6),
        "stddev": round(math.sqrt(var), 6),
    }


def ground_truth(items: list[dict]) -> dict:
    overall = _stats([item["e_gap"] for item in items])
    by_family = {}
    for family in FAMILIES:
        values = [item["e_gap"] for item in items if item["family"] == family]
        if values:
            by_family[family] = _stats(values)
    outliers = []
    if overall["stddev"] > 0:
        for item in items:
            if abs(item["e_gap"] - overall["mean"]) > 3 * overall["stddev"]:
                outliers.append(item["sample"])
    return {"overall": overall, "by_family": by_family, "outliers": outliers}


# --------------------------------------------------------------------------- programs (mcp mode)

SUMMARIZE_PROGRAM = (
    "import json,sys,time\n"
    "d=json.load(sys.stdin)\n"
    "items=d[\"items\"]\n"
    "first_index=int(items[0][\"sample\"].rsplit(\"-\",1)[-1]) if items else 0\n"
    "time.sleep(3.0 if first_index % 2 == 0 else 0.5)\n"
    "def st(xs):\n"
    "  n=len(xs); s=sum(xs); sq=sum(x*x for x in xs); m=s/n\n"
    "  return {\"count\":n,\"sum\":round(s,6),\"min\":round(min(xs),6),\"max\":round(max(xs),6),\"mean\":round(m,6),\"stddev\":round(max(0.0,sq/n-m*m)**0.5,6)}\n"
    "vals=[x[\"e_gap\"] for x in items]\n"
    "by={}\n"
    "for x in items:\n"
    "  by.setdefault(x[\"family\"],[]).append(x[\"e_gap\"])\n"
    "print(json.dumps({\"count\":len(vals),\"sum\":round(sum(vals),6),\"min\":round(min(vals),6),\"max\":round(max(vals),6),\"mean\":round(sum(vals)/len(vals),6),\"stddev\":round(max(0.0,sum(v*v for v in vals)/len(vals)-(sum(vals)/len(vals))**2)**0.5,6),\"by_family\":{k:st(v) for k,v in by.items()}}))\n"
)

MERGE_PROGRAM = (
    "import json,sys,math\n"
    "d=json.load(sys.stdin)\n"
    "ins=d[\"inputs\"]\n"
    "def combine(groups):\n"
    "  vals=[v for g in groups for v in g]\n"
    "  return {\"count\":len(vals),\"sum\":round(sum(vals),6),\"min\":round(min(vals),6),\"max\":round(max(vals),6),\"mean\":round(sum(vals)/len(vals),6),\"stddev\":round(max(0.0,sum(v*v for v in vals)/len(vals)-(sum(vals)/len(vals))**2)**0.5,6)}\n"
    "allv=[v for it in ins for v in [it[\"min\"],it[\"max\"]]]\n"
    "count=sum(it[\"count\"] for it in ins)\n"
    "total=sum(it[\"sum\"] for it in ins)\n"
    "sq=sum(it[\"count\"]*(it[\"stddev\"]**2+it[\"mean\"]**2) for it in ins)\n"
    "mean=total/count\n"
    "by={}\n"
    "for it in ins:\n"
    "  for k,v in it[\"by_family\"].items():\n"
    "    by.setdefault(k,{\"count\":0,\"sum\":0.0,\"min\":None,\"max\":None,\"sumsq\":0.0})\n"
    "    g=by[k]\n"
    "    g[\"count\"]+=v[\"count\"]; g[\"sum\"]+=v[\"sum\"]; g[\"sumsq\"]+=v[\"count\"]*(v[\"stddev\"]**2+v[\"mean\"]**2)\n"
    "    g[\"min\"]=v[\"min\"] if g[\"min\"] is None else min(g[\"min\"],v[\"min\"])\n"
    "    g[\"max\"]=v[\"max\"] if g[\"max\"] is None else max(g[\"max\"],v[\"max\"])\n"
    "byf={k:{\"count\":g[\"count\"],\"sum\":round(g[\"sum\"],6),\"min\":g[\"min\"],\"max\":g[\"max\"],\"mean\":round(g[\"sum\"]/g[\"count\"],6),\"stddev\":round(max(0.0,g[\"sumsq\"]/g[\"count\"]-(g[\"sum\"]/g[\"count\"])**2)**0.5,6)} for k,g in by.items()}\n"
    "overall={\"count\":count,\"sum\":round(total,6),\"min\":min(it[\"min\"] for it in ins),\"max\":max(it[\"max\"] for it in ins),\"mean\":round(mean,6),\"stddev\":round(max(0.0,sq/count-mean*mean)**0.5,6)}\n"
    "print(json.dumps({\"overall\":overall,\"by_family\":byf}))\n"
)


def _find_stats(value: Any) -> dict | None:
    if isinstance(value, dict):
        if all(k in value for k in ("count", "sum", "mean", "stddev")) and isinstance(value.get("count"), (int, float)):
            return value
        for v in value.values():
            found = _find_stats(v)
            if found:
                return found
    elif isinstance(value, list):
        for v in value:
            found = _find_stats(v)
            if found:
                return found
    return None


def _check_report(report: Any, gt: dict, passed: list[str]) -> None:
    if not isinstance(report, dict):
        raise RuntimeError(f"report is not an object: {report!r}")
    overall = report.get("overall") if isinstance(report.get("overall"), dict) else _find_stats(report)
    if overall is None:
        raise RuntimeError(f"could not locate aggregate stats in report: {json.dumps(report, ensure_ascii=False)[:400]}")
    exp = gt["overall"]
    for key in ("count", "sum", "min", "max", "mean", "stddev"):
        got = overall.get(key)
        want = exp[key]
        if not isinstance(got, (int, float)) or not math.isclose(float(got), float(want), rel_tol=1e-4, abs_tol=1e-4):
            raise RuntimeError(f"aggregate {key}: got {got!r}, want {want}")
    by_family = report.get("by_family")
    if isinstance(by_family, dict) and gt.get("by_family"):
        for family, want in gt["by_family"].items():
            got = by_family.get(family)
            if not isinstance(got, dict):
                raise RuntimeError(f"by_family[{family}] missing from report")
            for key in ("count", "sum", "min", "max", "mean", "stddev"):
                if not math.isclose(float(got[key]), float(want[key]), rel_tol=1e-4, abs_tol=1e-4):
                    raise RuntimeError(f"by_family[{family}].{key}: got {got[key]!r}, want {want[key]}")
    passed.append(f"G3: report matches ground truth (count={exp['count']}, mean={exp['mean']})")


def _check_provenance(run: dict, passed: list[str]) -> None:
    accepted = [e for e in run.get("events") or [] if e.get("phase") in {"node_accepted", "node_reassigned"}]
    completed = [e for e in run.get("events") or [] if e.get("phase") == "node_completed"]
    if not accepted or not completed:
        raise RuntimeError(f"no dynamic node events: accepted={len(accepted)} completed={len(completed)}")
    outcome = run.get("outcome") or {}
    ref = outcome.get("resource_ref")
    if not (isinstance(ref, dict) and content_digest(ref.get("resource_id", "")) is not None):
        raise RuntimeError(f"outcome not content-addressed: {ref!r}")
    node_ids = {n["node_id"] for n in run.get("dynamic_nodes") or []}
    accepted_ids = {e.get("node_id") for e in accepted}
    if node_ids != accepted_ids:
        raise RuntimeError("dynamic node set does not match node_accepted events")
    for n in run.get("dynamic_nodes") or []:
        if n.get("state") != "completed":
            raise RuntimeError(f"node not completed: {n.get('node_id')} state={n.get('state')}")
    passed.append(f"G5: provenance ok ({len(node_ids)} nodes, all completed, outcome content-addressed)")


def _check_failure_reassignment(
    run: dict[str, Any],
    controller: SlaveFailureController,
    passed: list[str],
) -> None:
    if not controller.injected:
        raise RuntimeError("G4 failure was not injected after partial progress")
    if run.get("execution_id") != controller.execution_id or int(run.get("execution_epoch", 0)) != controller.execution_epoch:
        raise RuntimeError("G4 reassignment changed the Run execution identity")
    attempts = run.get("attempts") or []
    events = [event for event in run.get("events") or [] if event.get("phase") == "node_reassigned"]
    for event in events:
        old_attempt = next(
            (item for item in attempts if item.get("attempt_id") == event.get("lost_attempt_id")),
            None,
        )
        replacement = next(
            (item for item in attempts if item.get("attempt_id") == event.get("replacement_attempt_id")),
            None,
        )
        authorization = event.get("reassignment_authorization") or {}
        if (
            old_attempt is not None
            and replacement is not None
            and old_attempt.get("state") == "lost"
            and replacement.get("state") == "completed"
            and old_attempt.get("node_id") == replacement.get("node_id")
            and old_attempt.get("target") == "slave-a"
            and replacement.get("target") != "slave-a"
            and authorization.get("policy") == "allow_reassignment"
            and authorization.get("value") is True
            and event.get("package_ref")
            and event.get("input_refs")
        ):
            healthy_sibling = any(
                item.get("node_id") != old_attempt.get("node_id") and item.get("state") == "completed"
                for item in attempts
            )
            if not healthy_sibling:
                raise RuntimeError("G4 has no healthy sibling completion")
            passed.append(
                "G4: slave-a loss was reassigned with audit and unchanged Run epoch; "
                "ground-truth equality validates this workload, not generic cross-target semantic equivalence"
            )
            return
    raise RuntimeError("G4 reassignment audit chain is incomplete")


def ref_dict(reference):
    return reference.model_dump(mode="json", include={"resource_id", "version_or_digest", "identity_criterion"}, exclude_none=True)


def package_ref(package):
    return ResourceRef(resource_id=package["resource"], version_or_digest=package["package_digest"])


async def prepare_plan(client, corpus, partitions, request_id, *, allow_reassignment=False, timeout=300):
    """Upload immutable artifacts and return the complete, reviewable plan."""
    if not 1 <= partitions <= len(corpus):
        raise ValueError("partitions must be between one and the corpus size")

    async def contract(input_keys, output_keys):
        refs = [await client.upload_content({"type": "object", "required": keys}, media_type="application/schema+json") for keys in (input_keys, output_keys)]
        return await client.upload_content({"schema_version": "io.v1", "input_schema_ref": ref_dict(refs[0]), "output_schema_ref": ref_dict(refs[1])}, media_type="application/vnd.loom.io-contract+json")

    async def descriptor(name):
        return await client.upload_content({"schema_version": "action.v1", "resource_id": client.names.name("actions", name), "name": name}, media_type="application/vnd.loom.action-descriptor+json")

    async def function(name, source, io_contract, action):
        program = await client.upload_content(source, media_type="text/x-python")
        package = await client.register_package({
            "package_id": f"{name}-{request_id}", "package_version": "v1", "package_type": "function",
            "execution": {"kind": "process:json_stdio", "version": "1"},
            "action_exports": [{"action_descriptor_ref": ref_dict(action), "io_contract_ref": ref_dict(io_contract),
                                    "effect_class": "Sandboxed", "permissions": [], "replay_safety": "SafeRead", "runtime_binding": {}}],
            "body": {"program_content_ref": ref_dict(program)},
        })
        return package_ref(package)

    summary_io = await contract(["items"], ["count", "by_family"])
    merge_io = await contract(["inputs"], ["overall", "by_family"])
    parent_io = await contract(["partitions"], ["overall", "by_family"])
    summary_descriptor = await descriptor("summarize-bandgap")
    merge_descriptor = await descriptor("merge-bandgap")
    parent_descriptor = await descriptor("orchestrate")
    summary = await function("summarize-bandgap", SUMMARIZE_PROGRAM, summary_io, summary_descriptor)
    merge = await function("merge-bandgap", MERGE_PROGRAM, merge_io, merge_descriptor)
    source = (
        f"SUMMARIZE = {ref_dict(summary)!r}\nSUMMARIZE_DESCRIPTOR = {ref_dict(summary_descriptor)!r}\n"
        f"MERGE = {ref_dict(merge)!r}\nMERGE_DESCRIPTOR = {ref_dict(merge_descriptor)!r}\n"
        'async def orchestrate(ctx: "OrchestrationContext", input_ref: "ResourceRef") -> "ResourceRef":\n'
        '    document = await ctx.read_json(input_ref)\n'
        '    partitions = document["partitions"]\n'
        '    summaries = []\n'
        '    for start in range(0, len(partitions), 2):\n'
        '        handles = [ctx.emit_node("part-" + str(i), SUMMARIZE, SUMMARIZE_DESCRIPTOR, [partitions[i]]) for i in range(start, min(start + 2, len(partitions)))]\n'
        '        for handle in handles:\n'
        '            summaries.append(await ctx.result(handle))\n'
        '    merged = ctx.emit_node("merge", MERGE, MERGE_DESCRIPTOR, summaries)\n'
        '    return await ctx.result(merged)\n'
    )
    program = await client.upload_content(source, media_type="text/x-python")
    parent = await client.register_package(OrchestratorPackage(
        program_content_ref=program, allowed_node_package_refs=[summary, merge],
        max_nodes=partitions + 1, max_live_nodes=min(2, partitions + 1),
        package_id=f"bandgap-parent-{request_id}", package_version="v1",
        action_descriptor_ref=parent_descriptor, io_contract_ref=parent_io,
    ).manifest())
    inputs = [ref_dict(await client.upload_content({"items": corpus[i::partitions]})) for i in range(partitions)]
    input_ref = await client.upload_content({"partitions": inputs})
    return SubmissionBuilder(
        goal="per-family band-gap statistics", workspace_id=client.workspace_id,
        body=TaskClosure(program={"action_ref": parent_descriptor.resource_id, "io_contract_ref": ref_dict(parent_io)}),
        orchestrator_package=package_ref(parent), input_binding=input_ref,
        resource_budget={"max_nodes": partitions + 1, "max_live_nodes": 2, "execution_timeout_seconds": timeout},
        recovery_policy={"allow_reassignment": allow_reassignment},
    ).build(request_id=request_id)


async def run(args):
    controller = SlaveFailureController(docker_bin=args.docker_bin, compose_file=args.compose_file) if args.inject_failure else None
    corpus = make_corpus(args.seed, args.files)
    request_id = "bandgap-" + uuid4().hex[:12]
    async with LoomClient(args.observer, token=os.environ["LOOM_GRIP_TOKEN"], grip_authority=args.authority,
                          workspace_id=args.workspace, allow_insecure_grip=args.allow_http) as client:
        slaves = await client.resources(mcue="computing/slave")
        if len([r for r in slaves if r["metadata"]["available"]]) < 2:
            raise RuntimeError("two available Slaves required")
        plan = await prepare_plan(client, corpus, args.partitions, request_id, allow_reassignment=controller is not None, timeout=args.timeout)
        queued = await client.submit(request_id=request_id, plan=plan)
        action = client.names.name("runs", queued["run_id"])
        try:
            deadline = time.monotonic() + args.timeout
            while time.monotonic() < deadline:
                result = await client.invoke(action, client.names.action("read"), {})
                if controller is not None:
                    controller.observe(await client.run(queued["run_id"]))
                if result["state"] in {"completed", "failed", "cancelled", "awaiting_decision"}:
                    break
                await asyncio.sleep(.5)
            else:
                raise TimeoutError(f"continue inspecting {action}")
            if result["state"] != "completed":
                raise RuntimeError(f"execution did not complete: {result}")
            passed = ["GRIP discovery and complete-plan execution"]
            _check_report(result["outcome"].get("value"), ground_truth(corpus), passed)
            run_record = await client.run(queued["run_id"])
            _check_provenance(run_record, passed)
            if controller is not None:
                _check_failure_reassignment(run_record, controller, passed)
            return {"action": action, "checks": passed, "outcome": result["outcome"]}
        finally:
            if controller is not None:
                controller.restore()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--observer", default=os.getenv("LOOM_OBSERVER_URL", "http://localhost:18080"))
    parser.add_argument("--authority", default=os.getenv("LOOM_GRIP_AUTHORITY", "loom.local"))
    parser.add_argument("--workspace", default=os.getenv("LOOM_WORKSPACE_ID", "workspace-default"))
    parser.add_argument("--allow-http", action="store_true")
    parser.add_argument("--files", type=int, default=60)
    parser.add_argument("--partitions", type=int, default=6)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--inject-failure", action="store_true", help="operator test: explicitly stop and restore slave-a via Docker")
    parser.add_argument("--docker-bin", default="docker")
    parser.add_argument("--compose-file", default="deploy/docker-compose.yml")
    args = parser.parse_args()
    if not 1 <= args.partitions <= args.files or args.timeout < 1:
        parser.error("require 1 <= partitions <= files and a positive timeout")
    print(json.dumps(asyncio.run(run(args)), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
