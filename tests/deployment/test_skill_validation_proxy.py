import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("proxy", Path(__file__).parents[2] / "scripts/skill_validation_proxy.py")
proxy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(proxy)
CONFIG = {"observer": "http://observer:8080", "grip_token": "operator-owner-secret",
          "model_base_url": "https://provider.example/v1", "model_api_key": "operator-model-secret", "session_token": "session"}


def test_only_proxy_injects_real_credential_and_does_not_follow_caller_urls():
    body = json.dumps({"auth": {"credential": "session"}, "method": "DESCRIBE"}).encode()
    url, forwarded, headers, model = proxy.prepare(CONFIG, "/.well-known/grip", body, "POST")
    assert url == "http://observer:8080/.well-known/grip"
    assert json.loads(forwarded)["auth"]["credential"] == CONFIG["grip_token"]
    assert CONFIG["grip_token"].encode() not in body
    for path in ["//evil.example/", "/internal/v1/agents", "/worker/v1/run", "/../internal/v1/agents", "/model/https://evil.example"]:
        with pytest.raises(ValueError):
            proxy.prepare(CONFIG, path, b'{}', "POST")
    url, _, headers, model = proxy.prepare(CONFIG, "/model/responses", b'{}', "POST")
    assert url == "https://provider.example/v1/responses"
    assert headers["Authorization"] == "Bearer " + CONFIG["model_api_key"]


def test_binding_rewrite_never_modifies_content_or_scoped_credentials():
    endpoint = CONFIG["observer"] + "/.well-known/grip"
    description = {"kind": "reply", "body": {"resource": {}, "offers": [{"bindings": [{"endpoint": endpoint}]}]}}
    assert proxy.rewrite_description(description, CONFIG["observer"], "http://127.0.0.1:8889")["body"]["offers"][0]["bindings"][0]["endpoint"].startswith("http://127.0.0.1")
    content = {"kind": "reply", "body": {"content": {"endpoint": endpoint}}}
    assert proxy.rewrite_description(content, CONFIG["observer"], "proxy") == content
    body = json.dumps({"auth": {"credential": "scoped-access-token"}}).encode()
    assert json.loads(proxy.prepare(CONFIG, "/.well-known/grip", body, "POST")[1])["auth"]["credential"] == "scoped-access-token"
