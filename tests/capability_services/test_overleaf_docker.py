"""Opt-in smoke test of the real image under the runtime's filesystem policy."""
import json
import os
import subprocess
import time
from uuid import uuid4

import pytest

IMAGE = os.environ.get("LOOM_OVERLEAF_TEST_IMAGE")
pytestmark = pytest.mark.skipif(not IMAGE, reason="set LOOM_OVERLEAF_TEST_IMAGE to a built Overleaf image")


def docker(*args, input=None, check=True):
    return subprocess.run(["docker", *args], input=input, text=True, capture_output=True, check=check, timeout=60)


def test_image_read_only_root_state_and_restart():
    name = "loom-overleaf-test-" + uuid4().hex[:12]
    volume = name + "-state"
    client = """
import httpx,json,sys
with httpx.Client(trust_env=False) as client:
    path,payload=json.load(sys.stdin)
    r=client.get('http://127.0.0.1:8080'+path) if payload is None else client.post('http://127.0.0.1:8080'+path,json=payload)
    r.raise_for_status()
    print(json.dumps(r.json()))
"""

    def request(path, payload=None):
        return json.loads(docker("exec", "-i", name, "python", "-c", client, input=json.dumps([path, payload])).stdout)

    def ready():
        deadline = time.monotonic()+20
        while True:
            try:
                assert request("/healthz") == {"status": "ok"}
                return
            except subprocess.CalledProcessError:
                if time.monotonic() >= deadline:
                    pytest.fail(docker("logs", name).stderr)
                time.sleep(.1)

    try:
        docker("volume", "create", volume)
        docker("run", "-d", "--name", name, "--network", "none", "--read-only", "--cap-drop", "ALL",
               "--security-opt", "no-new-privileges", "--memory", "512m", "--pids-limit", "128",
               "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m",
               "--mount", f"type=volume,source={volume},target=/var/lib/loom/service", IMAGE)
        ready()
        assert request("/v1/auth/status", {}) == {"status": "auth_required", "poll_after_seconds": 2}
        assert request("/v1/projects/status", {"project_id": "a"*24, "base_revision": "b"*40}) == {
            "status": "error", "result": None, "error_code": "git_credential_missing"}
        assert "git version" in docker("exec", name, "git", "--version").stdout
        wrote = docker("exec", name, "python", "-c", """
import os
from pathlib import Path
from capability_services.suanli_overleaf_http.common import Journal
j=Journal(Path('/var/lib/loom/service'))
j.put('smoke','fingerprint',{'state':'sending'})
assert os.getuid()==65534
assert j.path.stat().st_mode & 0o777 == 0o600
""")
        assert wrote.returncode == 0
        docker("restart", "--time", "1", name)
        ready()
        docker("exec", name, "python", "-c", """
from pathlib import Path
from capability_services.suanli_overleaf_http.common import Journal
assert Journal(Path('/var/lib/loom/service')).get('smoke','fingerprint')=={'state':'sending'}
""")
    finally:
        docker("rm", "-f", name, check=False)
        docker("volume", "rm", volume, check=False)
