from copy import deepcopy
from pathlib import Path
import sys

import pytest
from loom_orchestration.grip import Names

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from loom_business_skills import Memory, Store


class PublicClient:
    """Public-client contract double, deliberately independent of Loom server code."""
    def __init__(self, workspace="workspace-default", base_url="https://observer.example"):
        self.names, self.base_url = Names("loom.local", workspace), base_url
        self.token = "private-session-token-should-never-be-saved"
        self.calls, self.dispatches, self.errors = [], [], {}
        self.documents, self.descriptions = {}, {}
        self.add("catalog")
        self.add("metrics")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    def add(self, name):
        n = self.names
        target = n.name("activations", name, "v1", "slave-a", "named")
        package = n.name("packages", name, "v1")
        host = n.name("slaves", "slave-a")
        action = n.name("actions", "exports", name, "list")
        def ref(kind):
            resource = n.name("content", kind, "sha256", name + kind)
            self.documents[resource] = {"type": "object"}
            return {"resource_id": resource, "version_or_digest": name + kind}
        declaration = {"uri": action, "name": "list", "interaction": "request-reply", "available": True,
                       "permissions": [], "effect_class": "ReadOnly", "replay_safety": "DeclaredByPackage",
                       "action_descriptor_ref": ref("descriptor"), "io_contract_ref": ref("io"),
                       "arguments_schema": ref("input"), "result_schema": ref("output"),
                       "descriptor": {"name": "list", "schema_version": "action.v1"}}
        offer = {"action": {"uri": action, "mode": "request-reply", "input_schema": n.schema(name + "-input"),
                            "output_schema": n.schema(name + "-output")},
                 "bindings": [{"protocol": "https-json.v1", "endpoint": self.base_url + "/.well-known/grip", "audience": n.root}]}
        self.descriptions[target] = {"resource": {"uri": target, "mcue": "capability/activation", "metadata": {
            "package": package, "host": host, "target": None, "instance_name": "named", "revision": 1,
            "mcue_ref": n.mcue("capability/activation"), "state": "ready", "available": True,
            "desired_state": "running", "authentication": {"status": "authenticated"},
            "dependencies": [], "actions": [declaration]}}, "offers": [offer]}
        self.descriptions[package] = {"resource": {"uri": package, "mcue": "capability/package", "metadata": {
            "digest": name + "-digest-v1", "package_version": "v1"}}, "offers": []}
        self.descriptions[host] = {"resource": {"uri": host, "mcue": "computing/slave", "metadata": {
            "available": True, "dependencies": [], "attributes": {"host_resource": n.name("resources", "machine")}}}, "offers": []}
        return {"target": target, "action": action, "dependencies": []}

    async def describe(self, target, *, refresh=False):
        self.calls.append(("describe", target, refresh))
        if target in self.errors:
            raise self.errors[target]
        if target not in self.descriptions:
            from loom_orchestration.grip import GripError
            raise GripError("not_found", remote=True)
        return deepcopy(self.descriptions[target])

    async def read_json(self, ref):
        self.calls.append(("read_json", ref.resource_id))
        return deepcopy(self.documents[ref.resource_id])

    async def descriptor(self, ref):
        self.calls.append(("descriptor", ref.resource_id))
        return {"name": "list", "schema_version": "action.v1"}

    async def invoke(self, target, action, arguments):
        self.calls.append(("invoke", target, action))
        if action == self.names.action("how-to-use"):
            return {"sources": [target], "documentation": {"content_ref": self.names.name("content", "docs", "v1"),
                                                          "markdown": "Interpret current values using the public contract."}}
        if action == self.names.action("read"):
            return {"mcue": "capability/activation", "version": "v1", "attributes_schema": {"type": "object"}}
        self.dispatches.append((target, action, deepcopy(arguments)))
        return {"value": arguments.get("value", 0) * 2, "private": "RAW-BUSINESS-RESPONSE-DO-NOT-CACHE"}


def draft(client, key="paper-review", bindings=None):
    bindings = bindings or {"catalog": client.add("catalog")}
    return {"id": key, "title": "Paper venues 论文目录", "summary": "Read current venues for a literature task.",
            "keywords": ["papers", "venues", "论文"], "bindings": bindings,
            "steps": [{"id": name, "binding": name, "instruction": "Read the selected action with current parameters.",
                       "acceptance": "Check actual returned entries against the declared result schema.",
                       "on_failure": "Report the current blocker and rediscover if the contract changed."} for name in bindings]}


@pytest.fixture
def client():
    return PublicClient()


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "cache")


@pytest.fixture
def memory(store, client):
    return Memory(store, client)
