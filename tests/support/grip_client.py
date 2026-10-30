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


def resources(client, mcue):
    n = client.app.state.grip.names
    return invoke(client, n.root, n.action("list-resources"), {"mcue": mcue})["resources"]


def submit_plan(client, *, idempotency_key, plan, conversation_ref=None):
    n = client.app.state.grip.names
    return exchange(client, n.name("drivers", "default"), action=n.action("submit-plan"),
                    input={"idempotency_key": idempotency_key, "plan": plan,
                           **({"conversation_ref": conversation_ref} if conversation_ref is not None else {})})


def submission(client, idempotency_key):
    n = client.app.state.grip.names
    return invoke(client, n.name("drivers", "default"), n.action("get-submission"),
                  {"idempotency_key": idempotency_key})


def run_action(client, run_id, action="read", input=None):
    n = client.app.state.grip.names
    return invoke(client, n.name("runs", run_id), n.action(action), input)
