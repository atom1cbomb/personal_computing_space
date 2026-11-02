"""Opt-in acceptance of the real Ark image; live checks perform reads only."""
import json
import os
from pathlib import Path
import subprocess
import time
from uuid import uuid4

import pytest


IMAGE = os.getenv("LOOM_ARK_TEST_IMAGE")
KEY_FILE = os.getenv("LOOM_ARK_TEST_KEY_FILE")
pytestmark = pytest.mark.skipif(not IMAGE, reason="set LOOM_ARK_TEST_IMAGE to a built Ark image")
HTTP_CLIENT = """
import httpx,json,sys
call=json.load(sys.stdin)
with httpx.Client(timeout=20,trust_env=False) as client:
    r=client.request(call['method'],'http://127.0.0.1:8080'+call['path'],json=call['payload'])
print(json.dumps({'status':r.status_code,'body':r.json()}))
"""


def docker(*args, input=None, check=True):
    return subprocess.run(["docker", *args], input=input, text=True, capture_output=True,
                          check=check, timeout=60)


def request(name, path, payload=None, method="POST"):
    result = docker("exec", "-i", name, "python", "-c", HTTP_CLIENT,
                    input=json.dumps({"method": method, "path": path, "payload": payload}))
    return json.loads(result.stdout)


def ready(name):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        try:
            assert request(name, "/healthz", method="GET") == {"status": 200, "body": {"status": "ok"}}
            return
        except subprocess.CalledProcessError:
            time.sleep(.1)
    pytest.fail("Ark container did not become healthy")


def run(name, *options, network="none"):
    docker("run", "-d", "--name", name, "--network", network, "--read-only", "--cap-drop", "ALL",
           "--security-opt", "no-new-privileges", "--memory", "512m", "--pids-limit", "128",
           "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m", *options, IMAGE)


def test_real_image_nonroot_readonly_root_and_restart():
    name = "loom-ark-smoke-" + uuid4().hex[:12]
    try:
        run(name)
        ready(name)
        assert request(name, "/v1/auth/status", {}) == {"status": 200, "body": {"status": "auth_required"}}
        assert request(name, "/v1/videos/get", {"task_id": "../admin"})["status"] == 400
        assert request(name, "/v1/unknown", {})["status"] == 404
        docker("exec", name, "python", "-c", """
import errno,os
from pathlib import Path
assert os.getuid()==65534
try:
    Path('/app/unexpected-write').write_text('blocked')
except OSError as exc:
    assert exc.errno in (errno.EROFS,errno.EACCES)
else:
    raise AssertionError('root filesystem was writable')
""")
        docker("restart", "--time", "1", name)
        ready(name)
        assert request(name, "/v1/auth/start", {})["body"] == {"status": "auth_required"}
    finally:
        docker("rm", "-f", name, check=False)


@pytest.mark.skipif(not KEY_FILE, reason="set LOOM_ARK_TEST_KEY_FILE to explicitly enable live credential verification")
def test_real_image_live_key_authentication_and_video_list(tmp_path):
    name = "loom-ark-live-" + uuid4().hex[:12]
    config = tmp_path / "instance.json"
    config.write_text(json.dumps({"api_mode": os.getenv("LOOM_ARK_TEST_API_MODE", "agent-plan"),
                                  "target_resource_ref": None}))
    config.chmod(0o644)
    try:
        run(name, "--mount", f"type=bind,src={config},dst=/run/loom/instance.json,readonly",
            "--mount", f"type=bind,src={Path(KEY_FILE).resolve()},dst=/run/loom/credentials/api_key,readonly",
            network="bridge")
        ready(name)
        assert request(name, "/v1/auth/status", {}) == {"status": 200, "body": {"status": "authenticated"}}
        listed = request(name, "/v1/videos/list", {"page_num": 1, "page_size": 1})
        assert listed["status"] == 200 and isinstance(listed["body"]["result"]["items"], list)
        docker("restart", "--time", "1", name)
        ready(name)
        assert request(name, "/v1/auth/status", {})["body"] == {"status": "authenticated"}
    finally:
        docker("rm", "-f", name, check=False)
