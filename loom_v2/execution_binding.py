"""Operator-registered Slave execution action and its direct GRIP binding."""
from urllib.parse import urlsplit


def execution_offer(names, endpoint):
    endpoint = endpoint.rstrip("/")
    return {"action": {"uri": names.action("execute-attempt"), "mode": "request-reply",
        "input_schema": names.schema("execute-attempt-input"), "output_schema": names.schema("execute-attempt-output")},
        "bindings": [{"protocol": "https-json.v1" if urlsplit(endpoint).scheme == "https" else "loom-http-json.v1",
                      "audience": names.root, "endpoint": endpoint + "/.well-known/grip"}]}
