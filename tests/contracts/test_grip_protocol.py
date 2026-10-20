import json

import httpx
import pytest
from pydantic import ValidationError

from loom_orchestration import GripError, LoomClient, Names
from loom_orchestration.grip import Request, decode, uri


@pytest.mark.parametrize("value", [
    "grip://EXAMPLE.com/a", "grip://127.0.0.1/a", "grip://host:80/a",
    "grip://user@host/a", "grip://host/a?b", "grip://host/a#b", "grip://host/../a",
    "grip://host/%2E%2E/a", "grip://host/%61", "grip://host/%ff", "grip://host/%FF",
    "grip://host/中文", "grip://host/a\nb",
])
def test_noncanonical_uri_rejected(value):
    with pytest.raises(ValueError):
        uri(value)


def test_uri_keeps_encoded_separator_case_and_repeated_slashes():
    value = "grip://example.com/A//B%2FC/"
    assert uri(value) == value
    assert Names(workspace="a/b").root.endswith("a%2Fb")


@pytest.mark.parametrize("payload", [b'{"a":1,"a":2}', b'{"x":{"a":1,"a":2}}', b'{"x":NaN}', b'{"x":1e999}'])
def test_json_rejects_duplicate_keys_and_nonfinite_values(payload):
    with pytest.raises(ValueError):
        decode(payload)


@pytest.mark.parametrize("change", [
    {"accepted_protocols": ["https-json.v1", "https-json.v1"]},
    {"request_id": "x\n"}, {"version": 1}, {"input": {}}, {"auth": {"scheme": "bearer", "credential": 3}},
    {"deadline": "2026-10-19T00:00:00+08:00"}, {"auth": {"scheme": "bearer", "credential": "x", "extra": True}},
])
def test_wire_request_is_strict(change):
    base = {"version": "grip.v1", "request_id": "test", "method": "DESCRIBE", "target": Names().root,
            "auth": {"scheme": "bearer", "credential": "token"}, "accepted_protocols": ["https-json.v1"]}
    with pytest.raises(ValidationError):
        Request.model_validate({**base, **change})


@pytest.mark.parametrize("failure", ["correlation", "target", "expired", "version", "duplicate-action"])
async def test_sdk_rejects_untrusted_description(failure):
    n = Names()
    def respond(request):
        payload = json.loads(request.content)
        body = {"resource": {"uri": n.root, "kind": "workspace"}, "offers": []}
        reply = {"version": "grip.v1", "request_id": payload["request_id"], "kind": "reply", "body": body}
        if failure == "correlation": reply["request_id"] = "wrong"
        if failure == "target": body["resource"]["uri"] = n.name("other")
        if failure == "expired": body["expires_at"] = "2020-01-01T00:00:00Z"
        if failure == "version": reply["version"] = "grip.v2"
        if failure == "duplicate-action":
            offer = {"action": {"uri": n.action("read"), "mode": "request-reply", "input_schema": n.schema("read-input"), "output_schema": n.schema("read-output")}, "bindings": [{"protocol": "https-json.v1"}]}
            body["offers"] = [offer, offer]
        return httpx.Response(200, json=reply)
    async with LoomClient("https://observer.example", token="token", transport=httpx.MockTransport(respond)) as client:
        with pytest.raises((GripError, ValueError)):
            await client.describe(n.root)


@pytest.mark.parametrize("failure", ["endpoint", "audience", "redirect", "plain-http"])
async def test_sdk_never_forwards_token_to_untrusted_binding(failure):
    n = Names()
    requests = []
    def respond(request):
        requests.append(request)
        assert request.url.host == "observer.example"
        payload = json.loads(request.content)
        if failure == "redirect":
            return httpx.Response(307, headers={"location": "https://foreign.example/grip"})
        binding = {"protocol": "https-json.v1", "endpoint": "https://observer.example/grip"}
        if failure == "endpoint": binding["endpoint"] = "https://foreign.example/grip"
        if failure == "audience": binding["audience"] = "grip://foreign.example/service"
        if failure == "plain-http": binding["endpoint"] = "http://observer.example/grip"
        body = {"resource": {"uri": n.root, "kind": "workspace"}, "offers": [{
            "action": {"uri": n.action("read"), "mode": "request-reply", "input_schema": n.schema("read-input"), "output_schema": n.schema("read-output")},
            "bindings": [binding],
        }]}
        return httpx.Response(200, json={"version": "grip.v1", "request_id": payload["request_id"], "kind": "reply", "body": body})
    async with LoomClient("https://observer.example", token="token", transport=httpx.MockTransport(respond)) as client:
        with pytest.raises(GripError):
            await client.invoke(n.root, n.action("read"), {})
    assert len(requests) == 1
