"""Opt-in image acceptance against a real HTTPS fixture and fresh Docker volumes."""
from datetime import datetime, timedelta, timezone
import json
import os
import subprocess
import time
from uuid import uuid4

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
import pytest

IMAGE = os.environ.get("LOOM_AUTOPAPER_TEST_IMAGE")
pytestmark = pytest.mark.skipif(not IMAGE, reason="set LOOM_AUTOPAPER_TEST_IMAGE to a built AutoPaper image")

PROVIDER = '''
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import ssl
from urllib.parse import urlsplit, parse_qs

class Handler(BaseHTTPRequestHandler):
    def reply(self, status, data):
        raw = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def handle_request(self):
        control = json.loads(Path("/fixture/control.json").read_text())
        if control.get("status", 200) != 200:
            return self.reply(control["status"], {"detail": "private upstream error"})
        if self.headers.get("Cookie") != "ap_sid=docker-test-session":
            return self.reply(401, {"detail": "Not authenticated"})
        url = urlsplit(self.path)
        if self.command == "POST":
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if url.path != "/api/search" or body.get("auto_ingest") is not False or body.get("keywords") != ["RAG"]:
                return self.reply(422, {"detail": "unexpected search request"})
        if url.path in {"/api/papers/list", "/api/papers/years"}:
            if parse_qs(url.query).get("venue") != ["ACL & EMNLP"]:
                return self.reply(422, {"detail": "venue filter was not encoded correctly"})
        responses = json.loads(Path("/fixture/responses.json").read_text())
        if url.path not in responses:
            return self.reply(404, {})
        self.reply(200, responses[url.path])

    do_GET = handle_request
    do_POST = handle_request
    def log_message(self, *_args): pass

context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
context.load_cert_chain("/fixture/ca.pem", "/fixture/key.pem")
server = ThreadingHTTPServer(("0.0.0.0", 8443), Handler)
server.socket = context.wrap_socket(server.socket, server_side=True)
server.serve_forever()
'''

HTTP_CLIENT = '''
import httpx, json, sys
call = json.load(sys.stdin)
with httpx.Client(timeout=15, trust_env=False) as client:
    response = client.request(call["method"], "http://127.0.0.1:8080" + call["path"], json=call["payload"])
print(json.dumps({"status": response.status_code, "body": response.json()}))
'''


def docker(*args, input=None, check=True):
    return subprocess.run(["docker", *args], input=input, text=True, capture_output=True,
                          check=check, timeout=60)


def make_tls_fixture(directory):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "autopaper-provider")])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("autopaper-provider")]), critical=False)
            .sign(key, hashes.SHA256()))
    (directory / "ca.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (directory / "key.pem").write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    (directory / "provider.py").write_text(PROVIDER)
    (directory / "control.json").write_text('{"status": 200}')
    paper = {"id": "paper-1", "title": "Docker acceptance paper", "abstract": "Retrieval",
             "summary": {"problem": "Ranking", "method": "Embeddings", "impact": "Better retrieval"}}
    responses = {
        "/api/papers/venues": [{"venue": "ACL & EMNLP", "count": 1}],
        "/api/papers/years": [{"year": 2025, "count": 1}],
        "/api/papers/list": [paper], "/api/papers/paper-1": paper,
        "/api/search": {"session_id": "search-1", "total": 1, "papers": [paper]},
        "/api/venues": {"total": 1, "venues": [{"abbrev": "ACL", "full_names": ["ACL Conference"]}]},
    }
    (directory / "responses.json").write_text(json.dumps(responses))
    (directory / "instance.json").write_text(json.dumps({"api_origin": "https://autopaper-provider:8443"}))
    directory.chmod(0o755)
    for path in directory.iterdir():
        path.chmod(0o644)


def test_real_image_login_queries_fresh_volume_and_restart(tmp_path):
    make_tls_fixture(tmp_path)
    prefix = "loom-autopaper-test-" + uuid4().hex[:12]
    network, volume = prefix + "-network", prefix + "-state"
    provider_name, gateway_name, other_name = prefix + "-provider", prefix + "-gateway", prefix + "-other"

    def request(path, payload=None, *, method="POST", name=gateway_name, status=200):
        result = docker("exec", "-i", name, "python", "-c", HTTP_CLIENT,
                        input=json.dumps({"method": method, "path": path, "payload": payload or {}}))
        value = json.loads(result.stdout)
        assert value["status"] == status, value
        return value["body"]

    def wait_ready():
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            try:
                assert request("/healthz", method="GET") == {"status": "ok"}
                return
            except subprocess.CalledProcessError:
                time.sleep(.2)
        pytest.fail("gateway did not start: " + docker("logs", gateway_name).stdout)

    def login():
        sid = uuid4().hex
        assert request("/v1/auth/start", {"session_id": sid,
            "callback_uri": "http://localhost:3000/login-callback", "expires_at": time.time() + 300})["status"] == "auth_required"
        assert request("/v1/auth/browser", {"action": "open", "session_id": sid}) == {
            "status": "redirect", "location": "https://autopaper-provider:8443/oauth2/login"}
        callback = {"action": "callback", "session_id": sid, "query": {"ap_sid": ["docker-test-session"]}}
        assert request("/v1/auth/browser", callback) == {"status": "accepted"}
        request("/v1/auth/browser", callback, status=404)
        assert request("/v1/auth/status") == {"status": "authenticated"}

    try:
        certifi_path = docker("run", "--rm", "--network", "none", "--entrypoint", "python", IMAGE,
                              "-c", "import certifi; print(certifi.where())").stdout.strip()
        docker("network", "create", "--internal", network)
        docker("volume", "create", volume)
        docker("run", "-d", "--name", provider_name, "--network", network,
               "--network-alias", "autopaper-provider", "--read-only", "--cap-drop", "ALL",
               "--mount", f"type=bind,src={tmp_path},dst=/fixture,readonly",
               "--entrypoint", "python", IMAGE, "/fixture/provider.py")
        common = ["--network", network, "--read-only", "--user", "65534:65534", "--cap-drop", "ALL",
                  "--security-opt", "no-new-privileges", "--pids-limit", "128", "--memory", "512m",
                  "--cpus", "1", "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m",
                  "--mount", f"type=bind,src={tmp_path / 'instance.json'},dst=/run/loom/instance.json,readonly",
                  "--mount", f"type=bind,src={tmp_path / 'ca.pem'},dst={certifi_path},readonly"]

        def start():
            docker("run", "-d", "--name", gateway_name, *common,
                   "--mount", f"type=volume,source={volume},target=/var/lib/loom/service", IMAGE)
            wait_ready()

        start()
        assert request("/v1/auth/status")["status"] == "auth_required"
        request("/v1/venues/list", status=401)
        login()
        actions = {"/v1/papers/search": {"keywords": ["RAG"], "top_k": 5},
                   "/v1/papers/get": {"paper_id": "paper-1"},
                   "/v1/papers/list": {"venue": "ACL & EMNLP", "year": 2025},
                   "/v1/venues/list": {}, "/v1/years/list": {"venue": "ACL & EMNLP"},
                   "/v1/venues/catalog": {}}
        for path, payload in actions.items():
            assert request(path, payload)["result"]
        metadata = docker("exec", gateway_name, "python", "-c",
            "import os,json,pathlib; p=pathlib.Path('/var/lib/loom/service/autopaper-session.json'); "
            "print(json.dumps({'uid':os.getuid(),'owner':p.stat().st_uid,'mode':p.stat().st_mode & 0o777}))")
        assert json.loads(metadata.stdout) == {"uid": 65534, "owner": 65534, "mode": 0o600}
        docker("restart", "--time", "1", gateway_name)
        wait_ready()
        assert request("/v1/auth/status") == {"status": "authenticated"}
        docker("rm", "-f", gateway_name)
        start()
        assert request("/v1/auth/status") == {"status": "authenticated"}
        # A separate instance starts without the first instance's state.
        docker("run", "-d", "--name", other_name, *common, IMAGE)
        deadline = time.monotonic() + 25
        while True:
            try:
                assert request("/v1/auth/status", name=other_name)["status"] == "auth_required"
                break
            except subprocess.CalledProcessError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(.2)
        (tmp_path / "control.json").write_text('{"status": 401}')
        assert request("/v1/auth/status")["status"] == "auth_required"
        assert request("/v1/venues/list", status=401) == {"error": "autopaper_authentication_required"}
        (tmp_path / "control.json").write_text('{"status": 200}')
        login()
        assert request("/v1/papers/search", {"keywords": ["RAG"]})["result"]["total"] == 1
        (tmp_path / "control.json").write_text('{"status": 503}')
        assert request("/v1/auth/status")["status"] == "auth_failed"
        assert request("/v1/venues/list", status=502) == {"error": "autopaper_request_failed"}
        assert "docker-test-session" not in docker("logs", gateway_name).stdout
    finally:
        for name in (gateway_name, other_name, provider_name):
            docker("rm", "-f", name, check=False)
        docker("volume", "rm", volume, check=False)
        docker("network", "rm", network, check=False)
