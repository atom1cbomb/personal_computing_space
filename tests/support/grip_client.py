"""Synchronous protocol requests for API tests (no legacy REST adapter)."""
from uuid import uuid4


def exchange(client, target, *, action=None, input=None, **fields):
    service = client.app.state.grip
    service.settings.grip_token = "grip-test-token"
    message = {"version": "grip.v1", "request_id": uuid4().hex,
               "method": "INVOKE" if action else "DESCRIBE", "target": target,
               "auth": {"scheme": "bearer", "credential": "grip-test-token"},
               "accepted_protocols": [service.protocol], **fields}
    if action:
        message.update(action=action, input=input if input is not None else {})
    response = client.post("/.well-known/grip", json=message)
    assert response.status_code == 200, response.text
    return response.json()


def invoke(client, target, action, input=None):
    result = exchange(client, target, action=action, input=input)
    assert result["kind"] == "reply", result
    return result["body"]


def resources(client, kind):
    n = client.app.state.grip.names
    return invoke(client, n.root, n.action("list-resources"), {"kind": kind})["resources"]
