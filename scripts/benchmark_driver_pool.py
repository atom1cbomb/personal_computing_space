"""Bounded production load through the public SDK; credentials stay in config files.

Use an installed loom-orchestration wheel. Each task is a pure JSON function;
only this benchmark's Runs and activations are cleaned up. Results include raw
client timing and HTTP timing without request bodies or credentials. Server-side
queue timestamps should be collected separately to distinguish polling delay.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import time
from pathlib import Path
from uuid import uuid4

import httpx
from loom_orchestration import (
    ClosureContract,
    TaskClosure,
    LoomClient,
    publish_function,
)
from loom_orchestration.grip import GripError


def percentiles(xs):
    xs = sorted(xs)
    if not xs:
        return {}

    def p(q):
        i = (len(xs) - 1) * q
        a = int(i)
        return xs[a] + (xs[min(a + 1, len(xs) - 1)] - xs[a]) * (i - a)

    return {"p50": p(0.5), "p95": p(0.95), "max": xs[-1], "mean": statistics.mean(xs)}


class Benchmark:
    def __init__(self, args):
        self.args = args
        os.umask(0o077)
        self.out = Path(args.output)
        self.out.mkdir(parents=True, exist_ok=True)
        self.config = json.loads(Path(args.config).read_text())
        self.state = self.load(
            "state.json", {"runs": [], "activations": {}, "stages": []}
        )
        self.http_records = []

    def load(self, name, default):
        return (
            json.loads((self.out / name).read_text())
            if (self.out / name).exists()
            else default
        )

    def save(self, name, v):
        p = self.out / name
        t = p.with_suffix(p.suffix + ".tmp")
        t.write_text(json.dumps(v, indent=2, ensure_ascii=False))
        t.replace(p)

    def persist(self):
        self.save("state.json", self.state)

    async def request_hook(self, r):
        r.extensions["benchmark_started"] = time.perf_counter()

    async def response_hook(self, r):
        await r.aread()
        req = r.request
        try:
            body = json.loads(req.content)
        except (ValueError, UnicodeDecodeError):
            body = {}
        self.http_records.append(
            {
                "time": time.time(),
                "seconds": time.perf_counter() - req.extensions["benchmark_started"],
                "host": req.url.host,
                "port": req.url.port,
                "path": req.url.path,
                "method": body.get("method", req.method),
                "action": body.get("action"),
                "target": body.get("target"),
                "status": r.status_code,
            }
        )

    def client(self, http):
        c = self.config
        return LoomClient(
            c["observer"],
            token=c["token"],
            workspace_id=c["workspace"],
            grip_authority=c["authority"],
            client=http,
            timeout=30,
            connect_timeout=5,
            allow_insecure_grip=True,
            trusted_bindings=[
                {
                    "protocol": "loom-http-json.v1",
                    "endpoint": c["pool"] + "/.well-known/grip",
                    "audience": f"grip://{c['authority']}/workspaces/{c['workspace']}",
                }
            ],
        )

    async def setup(self, c):
        if self.state.get("package"):
            return
        source = "import sys,json,time,socket\nd=json.load(sys.stdin);start=time.time();time.sleep(d['delay']);print(json.dumps({'ticket':d['ticket'],'value':d['value']*2,'started':start,'finished':time.time(),'host':socket.gethostname()}))"
        inp = {
            "type": "object",
            "properties": {
                "ticket": {"type": "string"},
                "value": {"type": "integer"},
                "delay": {"type": "number", "minimum": 0, "maximum": 5},
            },
            "required": ["ticket", "value", "delay"],
            "additionalProperties": False,
        }
        out = {
            "type": "object",
            "properties": {
                "ticket": {"type": "string"},
                "value": {"type": "integer"},
                "started": {"type": "number"},
                "finished": {"type": "number"},
                "host": {"type": "string"},
            },
            "required": ["ticket", "value", "started", "finished", "host"],
            "additionalProperties": False,
        }
        p = await publish_function(
            c,
            package_id="driver-pool-benchmark",
            package_version="v" + uuid4().hex[:12],
            action_name="compute",
            source=source,
            input_schema=inp,
            output_schema=out,
        )
        self.state["package"] = p
        self.persist()
        available = [
            r["metadata"]["slave_id"]
            for r in await c.resources(mcue="computing/slave")
            if r["metadata"].get("available")
        ]
        for slave in self.args.slaves.split(","):
            assert slave in available, (slave, available)
            a = await c.invoke(
                c.names.name("slaves", slave),
                c.names.action("activate-package"),
                {
                    "package": p["resource"],
                    "instance_name": "benchmark",
                    "idempotency_key": "pool-bench-activate-"
                    + p["package_version"]
                    + "-"
                    + slave,
                },
            )
            meta = (await c.describe(a["resource"], refresh=True))["resource"][
                "metadata"
            ]
            assert meta["actions"][0]["execution"]["task"]["available"]
            self.state["activations"][slave] = {"receipt": a, "metadata": meta}
            self.persist()
        print("setup", len(self.state["activations"]), "slaves", flush=True)

    async def make_plan(self, c, label, index, delay):
        slave = list(self.state["activations"])[index % len(self.state["activations"])]
        meta = self.state["activations"][slave]["metadata"]
        export = meta["actions"][0]
        key = f"pool-bench-{label}-{uuid4().hex[:16]}"
        ar = export["action_descriptor_ref"]["resource_id"]
        ref = await c.upload_content(
            {"ticket": key, "value": index, "delay": delay}, name="pool-benchmark/input"
        )
        plan = ClosureContract(
            closure_id=key,
            goal="Driver Pool benchmark " + label,
            workspace_id=c.workspace_id,
            allowed_effects=export["permissions"],
            resource_budget={"execution_timeout_seconds": 120},
            recovery_policy={"allow_reassignment": False},
            body=TaskClosure(
                closure_id=key,
                program={
                    "action_ref": ar,
                    "io_contract_ref": export["io_contract_ref"],
                },
                compute={
                    "action_ref": ar,
                    "typed_holes": [
                        {
                            "hole_id": "target",
                            "status": "bound",
                            "binding_ref": "binding",
                        }
                    ],
                },
                compute_bindings=[
                    {
                        "binding_id": "binding",
                        "hole_id": "target",
                        "bound_by": "agent",
                        "action_descriptor_ref": export["action_descriptor_ref"],
                        "capability_package_ref": {
                            "resource_id": meta["package"],
                            "version_or_digest": self.state["package"][
                                "package_digest"
                            ],
                        },
                        "target_resource_ref": {"resource_id": meta["host"]},
                        "instance_name": meta["instance_name"],
                        "expected_activation_revision": meta["revision"],
                    }
                ],
                node_input_bindings=[
                    {"node_id": ar, "input_ref": ref.model_dump(mode="json")}
                ],
            ),
        )
        return {"key": key, "plan": plan, "value": index * 2, "slave": slave}

    async def batch(self, c):
        a = self.args
        label = a.label
        items = []
        for i in range(a.count):
            items.append(await self.make_plan(c, label, i, a.delay))
        self.save(
            label + "-plans.json",
            [{**x, "plan": x["plan"].model_dump(mode="json")} for x in items],
        )
        # Warm discovery and contract reads before measuring the stage.
        if self.state["runs"]:
            await c.submission(self.state["runs"][0]["key"])
        self.http_records = []
        sem = asyncio.Semaphore(a.concurrency)
        poll = asyncio.Semaphore(4)
        stop = asyncio.Event()
        results = []
        begin = time.time()

        async def job(item):
            async with sem:
                if stop.is_set() or (self.out / "STOP").exists():
                    return
                r = {"key": item["key"], "slave": item["slave"], "started": time.time()}
                results.append(r)
                try:
                    receipt = await c.submit(request_id=item["key"], plan=item["plan"])
                    r.update(run=receipt["run"], accepted=time.time())
                    self.state["runs"].append(
                        {"key": item["key"], "run": receipt["run"], "label": label}
                    )
                    self.persist()
                    deadline = time.monotonic() + a.max_wait
                    while True:
                        async with poll:
                            state = await c.run(r["run"])
                        if state["state"] in {
                            "completed",
                            "failed",
                            "cancelled",
                            "closed",
                            "awaiting_decision",
                            "awaiting_auth",
                        }:
                            break
                        if time.monotonic() > deadline:
                            raise TimeoutError("stage_wait_exceeded")
                        await asyncio.sleep(a.poll)
                    r.update(finished=time.time(), state=state["state"])
                    assert state["state"] == "completed", (
                        state["state"],
                        state.get("outcome"),
                    )
                    value = state["outcome"]["value"]
                    assert (
                        value["ticket"] == item["key"]
                        and value["value"] == item["value"]
                    )
                    r["business"] = value
                except Exception as exc:
                    r.update(
                        error=type(exc).__name__ + ":" + str(exc)[:250],
                        finished=time.time(),
                    )
                    stop.set()
                    if isinstance(exc, httpx.RequestError):
                        request = exc.request
                        try:
                            message = json.loads(request.content)
                        except (ValueError, UnicodeDecodeError):
                            message = {}
                        r["failed_request"] = {
                            "host": request.url.host,
                            "port": request.url.port,
                            "method": message.get("method", request.method),
                            "action": message.get("action"),
                            "target": message.get("target"),
                        }
                    if not r.get("run"):
                        try:
                            lookup = await c.submission(item["key"])
                            r["run"] = lookup["run"]
                            self.state["runs"].append(
                                {
                                    "key": item["key"],
                                    "run": lookup["run"],
                                    "label": label,
                                }
                            )
                            self.persist()
                        except Exception:
                            pass
                finally:
                    self.save(label + "-results.json", results)

        await asyncio.gather(*(job(x) for x in items))
        end = time.time()
        self.save(label + "-http.json", self.http_records)
        good = [
            r for r in results if r.get("state") == "completed" and not r.get("error")
        ]
        summary = {
            "label": label,
            "concurrency": a.concurrency,
            "requested": a.count,
            "started_count": len(results),
            "completed": len(good),
            "errors": [r for r in results if r.get("error")],
            "delay": a.delay,
            "started": begin,
            "finished": end,
            "seconds": end - begin,
            "observed_tasks_per_second": len(good) / (end - begin),
            "submit_seconds": percentiles([r["accepted"] - r["started"] for r in good]),
            "observed_e2e_seconds": percentiles(
                [r["finished"] - r["started"] for r in good]
            ),
            "http_requests": len(self.http_records),
        }
        self.save(label + "-summary.json", summary)
        self.state["stages"].append(summary)
        self.persist()
        print(json.dumps(summary), flush=True)
        if summary["errors"]:
            raise RuntimeError("benchmark_stopped_after_error")

    async def api(self, c):
        a = self.args
        entry = self.state["runs"][0]
        await c.run(entry["run"])
        await c.submission(entry["key"])
        self.http_records = []
        sem = asyncio.Semaphore(a.concurrency)
        rows = []
        stop = asyncio.Event()
        begin = time.time()

        async def one(i):
            async with sem:
                if stop.is_set() or (self.out / "STOP").exists():
                    return
                start = time.perf_counter()
                row = {"index": i}
                try:
                    if a.operation == "read":
                        value = await c.run(entry["run"])
                        assert value["state"] in {"completed", "closed"}
                    elif a.operation == "lookup":
                        value = await c.submission(entry["key"])
                        assert value["run"] == entry["run"]
                    elif a.operation == "duplicate":
                        plans = self.load(entry["label"] + "-plans.json", [])
                        original = next(x for x in plans if x["key"] == entry["key"])
                        value = await c.submit(
                            request_id=entry["key"],
                            plan=ClosureContract.model_validate(original["plan"]),
                        )
                        assert value["run"] == entry["run"]
                    else:
                        raise ValueError(a.operation)
                except Exception as e:
                    row["error"] = type(e).__name__ + ":" + str(e)[:200]
                    stop.set()
                row["seconds"] = time.perf_counter() - start
                rows.append(row)

        await asyncio.gather(*(one(i) for i in range(a.count)))
        end = time.time()
        self.save(a.label + "-results.json", rows)
        self.save(a.label + "-http.json", self.http_records)
        summary = {
            "label": a.label,
            "operation": a.operation,
            "concurrency": a.concurrency,
            "requested": a.count,
            "completed": sum("error" not in r for r in rows),
            "errors": [r for r in rows if "error" in r],
            "started": begin,
            "finished": end,
            "seconds": end - begin,
            "requests_per_second": len(rows) / (end - begin),
            "latency_seconds": percentiles(
                [r["seconds"] for r in rows if "error" not in r]
            ),
            "http_requests": len(self.http_records),
        }
        self.save(a.label + "-summary.json", summary)
        self.state["stages"].append(summary)
        self.persist()
        print(json.dumps(summary), flush=True)
        if summary["errors"]:
            raise RuntimeError("benchmark_stopped_after_error")

    async def reconcile(self, c):
        """Recover receipts for timed-out submissions before closing owned Runs."""
        known = {item["key"] for item in self.state["runs"]}
        recovered = []
        for path in sorted(self.out.glob("*-results.json")):
            label = path.name.removesuffix("-results.json")
            for result in json.loads(path.read_text()):
                key = result.get("key")
                if not key or key in known:
                    continue
                try:
                    receipt = await c.submission(key)
                except GripError as exc:
                    if exc.code != "not_found":
                        raise
                    recovered.append({"key": key, "state": "not_found"})
                    continue
                item = {"key": key, "run": receipt["run"], "label": label}
                self.state["runs"].append(item)
                known.add(key)
                recovered.append(item)
                self.persist()
        self.save("reconciliation.json", recovered)

    async def cleanup(self, c):
        await self.reconcile(c)
        closed = []
        for item in self.state["runs"]:
            r = await c.run(item["run"])
            if r["state"] in {"committed", "running", "cleaning_up"}:
                await c.cancel(item["run"])
                r = await c.run(item["run"])
            if r["state"] == "awaiting_decision":
                raise RuntimeError(
                    "benchmark_requires_explicit_decision:" + item["run"]
                )
            if r["state"] != "closed":
                await c.close(item["run"])
            closed.append(item["run"])
        for item in self.state["activations"].values():
            uri = item["receipt"]["resource"]
            meta = (await c.describe(uri, refresh=True))["resource"]["metadata"]
            if meta["desired_state"] != "stopped":
                await c.invoke(
                    uri,
                    c.names.action("stop"),
                    {
                        "expected_revision": meta["revision"],
                        "idempotency_key": "pool-bench-stop-" + uuid4().hex,
                    },
                )
        self.save(
            "cleanup.json",
            {"closed": closed, "stopped_activations": len(self.state["activations"])},
        )
        print("cleaned", len(closed), "runs", flush=True)

    async def main(self):
        async with httpx.AsyncClient(
            base_url=self.config["observer"],
            trust_env=False,
            timeout=30,
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=100),
            event_hooks={
                "request": [self.request_hook],
                "response": [self.response_hook],
            },
        ) as http:
            async with self.client(http) as c:
                if self.args.phase == "setup":
                    await self.setup(c)
                elif self.args.phase == "batch":
                    await self.batch(c)
                elif self.args.phase == "api":
                    await self.api(c)
                elif self.args.phase == "reconcile":
                    await self.reconcile(c)
                elif self.args.phase == "cleanup":
                    await self.cleanup(c)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("phase", choices=["setup", "batch", "api", "reconcile", "cleanup"])
    p.add_argument("--config", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--slaves", default="slave-a,slave-npc,slave-autopaper")
    p.add_argument("--label", default="warmup")
    p.add_argument("--count", type=int, default=2)
    p.add_argument("--concurrency", type=int, default=1)
    p.add_argument("--delay", type=float, default=0)
    p.add_argument("--poll", type=float, default=2)
    p.add_argument("--max-wait", type=float, default=180)
    p.add_argument(
        "--operation", choices=["read", "lookup", "duplicate"], default="read"
    )
    a = p.parse_args()
    if (
        not 1 <= a.concurrency <= 64
        or not 1 <= a.count <= 256
        or not 0 <= a.delay <= 5
        or a.poll < 0.5
    ):
        p.error("bounded limits: concurrency<=64, count<=256, delay<=5, poll>=0.5")
    asyncio.run(Benchmark(a).main())


if __name__ == "__main__":
    main()
