"""Fresh public GRIP descriptions, with immutable bodies read only at learning time."""
from __future__ import annotations

import httpx
from loom_orchestration import ResourceRef
from loom_orchestration.grip import GripError

from .model import Problem, environment, fingerprint


def issue(exc):
    if isinstance(exc, Problem):
        return {"code": exc.code, **exc.details}
    if isinstance(exc, (httpx.TransportError, TimeoutError, OSError)):
        return {"code": "offline"}
    if isinstance(exc, GripError):
        return {"code": "remote_error", "remote_code": exc.code}
    if isinstance(exc, httpx.HTTPStatusError):
        return {"code": "permission_denied" if exc.response.status_code in {401, 403} else "remote_http_error",
                "status_code": exc.response.status_code}
    # Do not surface exception strings: browser/provider errors can contain secrets.
    return {"code": "contract_unreadable"}


class Reader:
    def __init__(self, client):
        self.client = client
        self.seen = {}
        self.calls = 0
        self.root = environment(client)["workspace"]

    async def describe(self, target):
        if not target.startswith(self.root + "/"):
            raise Problem("foreign_resource")
        if target not in self.seen:
            if len(self.seen) >= 64:
                raise Problem("dependency_graph_limit")
            self.calls += 1
            value = await self.client.describe(target, refresh=True)
            if value["resource"]["uri"] != target:
                raise Problem("resource_identity_mismatch")
            self.seen[target] = value
        return self.seen[target]

    async def read(self, ref):
        self.calls += 1
        return await self.client.read_json(ResourceRef.model_validate(ref))

    async def invoke(self, target, action):
        self.calls += 1
        return await self.client.invoke(target, action, {})

    @staticmethod
    def available(resource):
        m = resource["metadata"]
        if m.get("state") in {"terminated", "stopped", "lost", "failed", "degraded", "provisioning", "pending_auth"}:
            raise Problem("resource_unavailable", state=m["state"])
        if m.get("available") is False:
            raise Problem("resource_unavailable", state=m.get("state"))
        auth = m.get("authentication") or {}
        if auth and auth.get("status") != "authenticated":
            raise Problem("authentication_required")
        if m.get("service_access", {}).get("issues"):
            raise Problem("binding_unavailable")

    async def graph(self, roots):
        graph, pending = {}, list(roots)
        while pending:
            ref = pending.pop()
            if ref in graph:
                continue
            r = (await self.describe(ref))["resource"]
            self.available(r)
            m = r["metadata"]
            links = m.get("dependencies")
            identity = {k: m.get(k) for k in ("mcue_ref", "package", "host", "target", "instance_name", "revision", "managed_by", "dependencies")}
            identity["attributes_fingerprint"] = fingerprint(m.get("attributes", {}))
            graph[ref] = {"mcue": r["mcue"], **identity}
            for link in links or []:
                pending.append(link["resource"])
        return graph

    async def binding(self, binding, *, bodies=False):
        target, selected = binding["target"], binding["action"]
        d = await self.describe(target)
        r, m = d["resource"], d["resource"]["metadata"]
        if r["mcue"] != "capability/activation":
            candidates = m.get("activations") or []
            raise Problem("named_activation_required", candidates=candidates[:5], candidate_count=len(candidates))
        self.available(r)
        actions = [a for a in m.get("actions", []) if a["uri"] == selected]
        offers = [o for o in d["offers"] if o["action"]["uri"] == selected]
        if len(actions) != 1 or len(offers) != 1:
            raise Problem("action_unavailable")
        action, offer = actions[0], offers[0]
        if action.get("available") is False:
            raise Problem("action_unavailable")
        if action.get("interaction") not in {"task", "request-reply", "byte-stream"}:
            raise Problem("interaction_missing")
        p = (await self.describe(m["package"]))["resource"]["metadata"]
        if not p.get("digest"):
            raise Problem("package_digest_missing")
        roots = [target, m["host"], *binding.get("dependencies", [])]
        if m.get("target"):
            roots.append(m["target"])
        graph = await self.graph(roots)
        contract = {
            "package": m["package"], "package_digest": p["digest"], "package_version": p.get("package_version"),
            "action": {k: action.get(k) for k in ("uri", "name", "interaction", "effect_class", "permissions", "replay_safety",
                                                  "action_descriptor_ref", "io_contract_ref", "arguments_schema", "result_schema")},
            "wire_action": offer["action"], "bindings": offer["bindings"], "dependencies": graph,
        }
        # Token/lease lifetimes are excluded; grants and activation revisions are checked.
        access = m.get("service_access") or {}
        contract["access_contract"] = {k: access[k] for k in ("actions", "dependencies", "grants") if k in access}
        value = {"contract": contract, "local_fingerprint": fingerprint(contract)}
        if bodies:
            usage = await self.invoke(target, self.client.names.action("how-to-use"))
            schemas = {name: await self.read(action[name]) for name in ("arguments_schema", "result_schema")}
            descriptor = action.get("descriptor")
            if descriptor is None:
                self.calls += 1
                descriptor = await self.client.descriptor(ResourceRef.model_validate(action["action_descriptor_ref"]))
            definition = await self.invoke(m["mcue_ref"], self.client.names.action("read"))
            value["bodies"] = {**schemas, "descriptor": descriptor, "mcue": definition}
            documentation = usage.get("documentation") or {}
            value["sources"] = {"documentation_ref": documentation.get("content_ref"),
                                "documentation_fingerprint": fingerprint(documentation.get("markdown", "")),
                                "references": usage.get("sources", [])}
        return value
