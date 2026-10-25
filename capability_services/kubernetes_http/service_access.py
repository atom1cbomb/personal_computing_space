"""Owned Kubernetes Services and supervised private port-forward channels."""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import random
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from loom_orchestration.grip import GripError, uri
from loom_orchestration.bindings import timestamp
from loom_orchestration.stream_server import OpenedStream
from loom_orchestration.streams import TCPStream

OWNER = "loom.cs.ac.cn/service-ref"
READY = re.compile(r"Forwarding from 127\.0\.0\.1:(\d+) -> \d+")


class AccessError(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def classify(stderr, timed_out=False):
    message = stderr.lower()
    if "forbidden" in message:
        return "rbac_denied"
    if any(word in message for word in ("unauthorized", "provide credentials", "you must be logged in", "please visit", "invalid_grant", "token is expired")):
        return "pending_auth"
    if "notfound" in message or "not found" in message:
        return "service_unavailable"
    if timed_out or any(word in message for word in ("timeout", "timed out", "connection", "eof", "no route", "network")):
        return "reconnecting"
    return "configuration_error"


class ServiceAccess:
    def __init__(self, gateway, config=None):
        self.gateway = gateway
        if config is None:
            path = Path(os.getenv("LOOM_INSTANCE_CONFIG", "/run/loom/instance.json"))
            config = json.loads(path.read_text()) if path.exists() else {}
        self.targets = config.get("service_targets", {})
        self.state_path = gateway.home / "service-identities.json"
        self.identities = json.loads(self.state_path.read_text()) if self.state_path.exists() else {}
        self.channels = {}
        self.locks = {}
        self.closed = False
        for ref, target in self.targets.items():
            uri(ref)
            if target.get("namespace") != gateway.policy["namespace"]:
                raise AccessError("namespace_forbidden")
            gateway._name(target["name"])
            if type(target["port"]) is not int or not 1 <= target["port"] <= 65535:
                raise AccessError("configuration_error")
            workload = target["workload"]
            if workload["resource"] not in {"vcjob", "deployment", "job"} or not workload["uid"]:
                raise AccessError("configuration_error")
            gateway._name(workload["name"])
            if not target.get("selector") or not all(isinstance(k, str) and isinstance(v, str) for k, v in target["selector"].items()):
                raise AccessError("configuration_error")

    def target(self, ref):
        if ref not in self.targets:
            raise AccessError("service_not_allowed")
        return self.targets[ref]

    async def command(self, arguments, *, value=None, missing=False):
        code, stdout, stderr, timed_out = await asyncio.to_thread(self.gateway._run,
            [*self.gateway._base_args(), *arguments], stdin=json.dumps(value).encode() if value is not None else None)
        if code or timed_out:
            if missing and "notfound" in stderr.lower():
                return None
            raise AccessError(classify(stderr, timed_out))
        try:
            return json.loads(stdout)
        except ValueError as exc:
            raise AccessError("kubernetes_response_invalid") from exc

    async def check_workload(self, target):
        expected = target["workload"]
        workload = await self.command(["get", expected["resource"], expected["name"], "-o", "json"])
        if workload["metadata"]["uid"] != expected["uid"]:
            raise AccessError("workload_identity_changed")
        if expected["resource"] == "vcjob":
            labels = {"volcano.sh/job-name": expected["name"]}
        else:
            labels = workload.get("spec", {}).get("template", {}).get("metadata", {}).get("labels", {})
        if not any(target["selector"].get(k) == v for k, v in labels.items()) or any(labels.get(k) != v for k, v in target["selector"].items()):
            raise AccessError("workload_selector_mismatch")

    @staticmethod
    def owner_reference(target):
        workload = target["workload"]
        api, kind = {"vcjob": ("batch.volcano.sh/v1alpha1", "Job"),
                     "deployment": ("apps/v1", "Deployment"), "job": ("batch/v1", "Job")}[workload["resource"]]
        return {"apiVersion": api, "kind": kind, "name": workload["name"], "uid": workload["uid"]}

    def validate_service(self, ref, target, service, *, require_saved=True):
        metadata, spec = service.get("metadata", {}), service.get("spec", {})
        saved = self.identities.get(ref)
        if metadata.get("annotations", {}).get(OWNER) != ref or (require_saved and not saved) or (saved and saved != metadata.get("uid")):
            raise AccessError("service_identity_changed")
        owner = self.owner_reference(target)
        if not any(all(actual.get(k) == v for k, v in owner.items()) for actual in metadata.get("ownerReferences", [])):
            raise AccessError("workload_identity_changed")
        if (metadata.get("namespace") != target["namespace"] or metadata.get("name") != target["name"]
                or spec.get("type", "ClusterIP") != "ClusterIP" or spec.get("clusterIP") in {None, "None", ""}
                or spec.get("externalIPs") or spec.get("externalName") or spec.get("selector") != target["selector"]):
            raise AccessError("service_configuration_mismatch")
        ports = spec.get("ports", [])
        if len(ports) != 1 or ports[0].get("port") != target["port"] or ports[0].get("targetPort", target["port"]) != target["port"] or ports[0].get("protocol", "TCP") != "TCP":
            raise AccessError("service_configuration_mismatch")

    async def ensure(self, ref):
        target = self.target(ref)
        async with self.locks.setdefault(ref, asyncio.Lock()):
            await self.check_workload(target)
            existing = await self.command(["get", "service", target["name"], "-o", "json"], missing=True)
            if existing is None:
                if ref in self.identities:
                    raise AccessError("service_identity_changed")
                manifest = {"apiVersion": "v1", "kind": "Service", "metadata": {
                    "name": target["name"], "namespace": target["namespace"], "annotations": {OWNER: ref},
                    "ownerReferences": [self.owner_reference(target)]},
                    "spec": {"type": "ClusterIP", "selector": target["selector"],
                             "ports": [{"name": "http", "port": target["port"], "targetPort": target["port"], "protocol": "TCP"}]}}
                existing = await self.command(["create", "-f", "-", "-o", "json"], value=manifest)
            metadata, spec = existing.get("metadata", {}), existing.get("spec", {})
            saved = self.identities.get(ref)
            if (metadata.get("annotations", {}).get(OWNER) != ref or (saved and saved != metadata.get("uid"))
                    or not metadata.get("uid") or metadata.get("namespace") != target["namespace"] or metadata.get("name") != target["name"]):
                raise AccessError("service_identity_changed")
            owner = self.owner_reference(target)
            if not any(all(actual.get(k) == v for k, v in owner.items()) for actual in metadata.get("ownerReferences", [])):
                raise AccessError("workload_identity_changed")
            if spec.get("type", "ClusterIP") != "ClusterIP" or spec.get("clusterIP") in {None, "None", ""} or spec.get("externalIPs") or spec.get("externalName"):
                raise AccessError("service_configuration_mismatch")
            desired_ports = [{"name": "http", "port": target["port"], "targetPort": target["port"], "protocol": "TCP"}]
            if spec.get("selector") != target["selector"] or spec.get("ports") != desired_ports:
                # resourceVersion/UID makes replacement conditional. Do not
                # recreate on conflict or adopt a concurrently replaced name.
                existing["spec"] = {**spec, "selector": target["selector"], "ports": desired_ports}
                existing = await self.command(["replace", "-f", "-", "-o", "json"], value=existing)
            self.validate_service(ref, target, existing, require_saved=False)
            self.identities[ref] = existing["metadata"]["uid"]
            temporary = self.state_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(self.identities))
            temporary.replace(self.state_path)
            self.start(ref)
            return {"status": "ensured", "service_ref": ref, "namespace": target["namespace"], "name": target["name"], "uid": existing["metadata"]["uid"], "state": self.status(ref)["state"]}

    def start(self, ref):
        if ref not in self.channels:
            self.channels[ref] = {"state": "connecting", "port": None, "process": None, "streams": set(), "task": None, "checked_at": None}
        channel = self.channels[ref]
        if channel["task"] is None or channel["task"].done():
            channel["task"] = asyncio.create_task(self.supervise(ref))

    async def start_saved(self):
        for ref in self.targets.keys() & self.identities.keys():
            self.start(ref)

    async def _stop_process(self, process):
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 3)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()

    async def supervise(self, ref):
        channel, target = self.channels[ref], self.target(ref)
        delay = 1
        while not self.closed:
            process = reader = waiter = None
            try:
                channel["checked_at"] = datetime.now(timezone.utc).isoformat()
                authentication = await asyncio.to_thread(self.gateway._require_auth)
                if authentication is not None:
                    # Transport/probe failures wait for the gateway's retry;
                    # only an actual login requirement is pending_auth.
                    raise AccessError("pending_auth" if authentication["status"] == "auth_required" else "reconnecting")
                await self.check_workload(target)
                service = await self.command(["get", "service", target["name"], "-o", "json"])
                self.validate_service(ref, target, service)
                process = await asyncio.create_subprocess_exec(*self.gateway._base_args(), "port-forward",
                    "--address", "127.0.0.1", "service/" + target["name"], ":" + str(target["port"]),
                    stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                    env={"PATH": os.getenv("PATH", ""), "HOME": str(self.gateway.home), "KUBECONFIG": str(self.gateway.kubeconfig)}, limit=65536)
                channel["process"] = process
                ready = asyncio.Event()
                async def output():
                    while line := await process.stdout.readline():
                        text = line.decode(errors="replace").strip()
                        match = READY.fullmatch(text)
                        if match:
                            channel.update(state="ready", port=int(match[1]))
                            ready.set()
                        elif text and not re.fullmatch(r"Handling connection for \d+", text):
                            channel["state"] = classify(text)
                reader = asyncio.create_task(output())
                waiter = asyncio.create_task(process.wait())
                readiness = asyncio.create_task(ready.wait())
                try:
                    done, _ = await asyncio.wait([reader, waiter, readiness], timeout=30, return_when=asyncio.FIRST_COMPLETED)
                    if readiness not in done:
                        raise AccessError(channel["state"] if waiter in done and channel["state"] not in {"connecting", "ready"} else "reconnecting")
                finally:
                    readiness.cancel()
                    await asyncio.gather(readiness, return_exceptions=True)
                delay = 1
                # Revalidate identity periodically: a Service name can be reused.
                while process.returncode is None:
                    done, _ = await asyncio.wait([waiter, reader], timeout=10, return_when=asyncio.FIRST_COMPLETED)
                    if done:
                        break
                    channel["checked_at"] = datetime.now(timezone.utc).isoformat()
                    await self.check_workload(target)
                    self.validate_service(ref, target, await self.command(["get", "service", target["name"], "-o", "json"]))
                if channel["state"] == "ready":
                    channel["state"] = "reconnecting"
            except AccessError as exc:
                channel["state"] = exc.code
            except (OSError, ValueError, asyncio.TimeoutError):
                channel["state"] = "reconnecting"
            finally:
                channel["port"] = None
                for stream in list(channel["streams"]):
                    await stream.aclose(1011)
                if process:
                    await self._stop_process(process)
                for task in (reader, waiter):
                    if task:
                        task.cancel()
                await asyncio.gather(*(t for t in (reader, waiter) if t), return_exceptions=True)
                channel["process"] = None
            # Only transport and authentication failures are recoverable.
            if channel["state"] not in {"reconnecting", "pending_auth", "service_unavailable"}:
                return
            await asyncio.sleep(delay * random.uniform(0.8, 1.0))
            delay = min(delay * 2, 30)

    def status(self, ref):
        self.target(ref)
        channel = self.channels.get(ref)
        observed = channel["state"] if channel else "not_configured"
        state = observed if observed in {"ready", "not_configured", "connecting", "reconnecting", "pending_auth"} else "reconnecting" if observed == "service_unavailable" else "blocked" if observed == "rbac_denied" else "failed"
        return {"service_ref": ref, "state": state, "last_error": observed if observed not in {"ready", "connecting", "not_configured"} else None,
                "checked_at": channel.get("checked_at") if channel else None}

    async def open(self, request):
        ref = request.input.get("service_ref")
        try:
            target = self.target(ref)
            channel = self.channels.get(ref)
            deadline = getattr(request, "deadline", None)
            budget = min(30, timestamp(deadline) - time.time()) if deadline else 30
            end = time.monotonic() + max(0, budget)
            while channel and channel["state"] in {"connecting", "reconnecting", "service_unavailable"}:
                if time.monotonic() >= end:
                    raise GripError("deadline_exceeded")
                await asyncio.sleep(min(.05, end - time.monotonic()))
            if channel and channel["state"] == "pending_auth":
                raise GripError("authentication_required")
            if not channel or channel["state"] != "ready" or channel["port"] is None:
                raise AccessError("service_unavailable")
            # Admission uses the saved UID, not merely a matching name.
            self.validate_service(ref, target, await self.command(["get", "service", target["name"], "-o", "json"]))
            reader, writer = await asyncio.open_connection("127.0.0.1", channel["port"])
            upstream = TCPStream(reader, writer)
            class TrackedStream:
                async def read(self, n):
                    return await upstream.read(n)
                async def write(self, data):
                    await upstream.write(data)
                async def aclose(self, code=1000):
                    channel["streams"].discard(self)
                    await upstream.aclose(code)
            stream = TrackedStream()
            channel["streams"].add(stream)
            return OpenedStream(stream, {})
        except AccessError as exc:
            raise GripError("forbidden" if exc.code == "service_not_allowed" else "execution_failed") from exc

    async def close(self):
        self.closed = True
        tasks = [c["task"] for c in self.channels.values() if c["task"]]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
