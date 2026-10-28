"""Run one skill-only Codex case in a fresh local Docker container.

Operator-side tooling: never copy this script or the repository into the agent.
Run as root on the Docker host; only its container network namespace is modified.
See docs/validation/skill-only-codex.md for tasks and independent acceptance.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import subprocess
import tarfile
import time
import uuid
import zipfile
from urllib.parse import urlsplit


BASE_CONFIG = '''approval_policy = "never"
sandbox_mode = "danger-full-access"
web_search = "disabled"
project_doc_max_bytes = 0
[features]
apps = false
plugins = false
multi_agent = false
shell_snapshot = false
[shell_environment_policy]
exclude = ["LOOM_VALIDATION_MODEL_API_KEY"]
'''

# No model call: inspect the CLI's actual skill catalog, including bundled skills.
CATALOG_SCRIPT = r'''
import json, select, subprocess, time
p = subprocess.Popen(["codex", "app-server"], stdin=subprocess.PIPE,
                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
buffer = b""
def rpc(ident, method, params):
    global buffer
    p.stdin.write((json.dumps({"id": ident, "method": method, "params": params})+"\n").encode())
    p.stdin.flush()
    deadline = time.monotonic()+30
    while time.monotonic() < deadline:
        if b"\n" not in buffer:
            if not select.select([p.stdout], [], [], max(0, deadline-time.monotonic()))[0]:
                break
            chunk = p.stdout.read1(65536)
            if not chunk:
                break
            buffer += chunk
            continue
        line, buffer = buffer.split(b"\n", 1)
        message = json.loads(line)
        if message.get("id") == ident:
            if "error" in message:
                raise RuntimeError(message["error"])
            return message["result"]
    raise RuntimeError("skills/list timed out")
try:
    rpc(1, "initialize", {"clientInfo": {"name": "loom-skill-validation", "version": "1"}})
    print(json.dumps(rpc(2, "skills/list", {"cwds": ["/work"], "forceReload": True})))
finally:
    p.terminate()
    p.wait(timeout=5)
'''

BOUNDARY_SCRIPT = r'''
import importlib.util, json, os, pathlib, shutil
checks = {
    "non_root": os.getuid() == 1000,
    "empty_workspace": not any(pathlib.Path("/work").iterdir()),
    "no_server_module": importlib.util.find_spec("loom_v2") is None,
    "no_ssh_client": shutil.which("ssh") is None,
    "no_ssh_agent": "SSH_AUTH_SOCK" not in os.environ,
    "no_docker_socket": not pathlib.Path("/var/run/docker.sock").exists(),
    "no_user_ssh_directory": not pathlib.Path.home().joinpath(".ssh").exists(),
    "no_sudo": shutil.which("sudo") is None,
}
print(json.dumps(checks))
assert all(checks.values()), "isolation preflight failed"
'''


def command(*args: str, input: bytes | None = None, timeout: int = 120) -> bytes:
    return subprocess.run(args, input=input, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, check=True, timeout=timeout).stdout


def skill_files(bundle: Path) -> dict[str, bytes]:
    """Accept the release bundle, not a checkout or a directory of other skills."""
    files = {}
    with zipfile.ZipFile(bundle) as archive:
        for entry in archive.infolist():
            path = PurePosixPath(entry.filename)
            if (path.is_absolute() or ".." in path.parts or not path.parts
                    or path.parts[0] != "personal-computing-space"
                    or stat.S_ISLNK(entry.external_attr >> 16)):
                raise ValueError("Unexpected skill archive entry")
            if not entry.is_dir():
                files[str(PurePosixPath(".agents/skills") / path)] = archive.read(entry)
    if ".agents/skills/personal-computing-space/SKILL.md" not in files:
        raise ValueError("Skill is missing SKILL.md")
    wheels = [data for name, data in files.items()
              if "/assets/wheels/loom_orchestration-" in name and name.endswith(".whl")]
    if len(wheels) != 1:
        raise ValueError("Build the release skill with exactly one SDK wheel")
    with zipfile.ZipFile(io.BytesIO(wheels[0])) as wheel:
        if any(name.startswith("loom_v2/") for name in wheel.namelist()):
            raise ValueError("Server modules are forbidden in the skill SDK")
    return files


def put_files(container: str, files: dict[str, bytes]) -> None:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        for name, data in files.items():
            entry = tarfile.TarInfo(name)
            entry.size, entry.mode, entry.uid, entry.gid = len(data), 0o600, 1000, 1000
            archive.addfile(entry, io.BytesIO(data))
    command("docker", "exec", "-i", container, "tar", "xf", "-", "-C", "/home/agent",
            input=stream.getvalue())


def start_credential_proxy(container: str, observer: str, token: bytes, provider: dict, session_token: str) -> None:
    """Keep real credentials owned by a different UID from the tested agent."""
    config = {"observer": observer, "grip_token": token.decode(), "session_token": session_token,
              "model_base_url": provider["base_url"], "model_api_key": provider["api_key"]}
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w") as archive:
        for name, data in {"config.json": json.dumps(config).encode(),
                           "proxy.py": Path(__file__).with_name("skill_validation_proxy.py").read_bytes()}.items():
            entry = tarfile.TarInfo(name)
            entry.size, entry.mode, entry.uid, entry.gid = len(data), 0o600, 0, 0
            archive.addfile(entry, io.BytesIO(data))
    command("docker", "exec", "--user", "0", "-i", container, "tar", "xf", "-", "-C", "/run/loom-operator",
            input=stream.getvalue())
    command("docker", "exec", "--user", "0", "--detach", container, "python", "/run/loom-operator/proxy.py",
            "/run/loom-operator/config.json")
    command("docker", "exec", container, "python", "-c", """
import pathlib,time,urllib.request
try:
    pathlib.Path('/run/loom-operator/config.json').read_bytes()
except PermissionError:
    pass
else:
    raise AssertionError('operator credentials readable by agent')
for attempt in range(30):
    try:
        with urllib.request.urlopen('http://127.0.0.1:8889/healthz', timeout=5) as r:
            assert r.status == 200
        break
    except OSError:
        time.sleep(.2)
else:
    raise RuntimeError('credential proxy unavailable')
""")


def catalog(container: str) -> dict:
    return json.loads(command("docker", "exec", container, "python", "-c", CATALOG_SCRIPT))


def isolate_skills(container: str, base_config: str = BASE_CONFIG) -> dict:
    initial = catalog(container)
    config = base_config
    for directory in initial["data"]:
        if directory.get("errors"):
            raise ValueError("Codex reported skill-loading errors")
        for skill in directory["skills"]:
            if skill["path"] != "/home/agent/.agents/skills/personal-computing-space/SKILL.md":
                config += "\n[[skills.config]]\npath = " + json.dumps(skill["path"]) + "\nenabled = false\n"
    put_files(container, {".codex/config.toml": config.encode()})
    final = catalog(container)
    active = [s for d in final["data"] for s in d["skills"] if s["enabled"]]
    if (any(d.get("errors") for d in final["data"]) or len(active) != 1
            or active[0]["path"] != "/home/agent/.agents/skills/personal-computing-space/SKILL.md"):
        raise ValueError("Expected exactly one enabled project skill")
    return final


def provider_inputs(path: Path) -> tuple[str, bytes, dict]:
    """Accept only model connection data, never a host Codex configuration."""
    value = json.loads(path.read_text())
    required = {"model", "base_url", "wire_api", "api_key"}
    if (set(value) != required or any(not isinstance(v, str) or not v for v in value.values())
            or value["wire_api"] != "responses"):
        raise ValueError("Provider file requires only model, base_url, wire_api=responses and api_key")
    url = urlsplit(value["base_url"])
    if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise ValueError("Provider base_url must be an HTTP(S) origin/path without embedded credentials")
    config = "model = " + json.dumps(value["model"]) + '\nmodel_provider = "validation"\n' + BASE_CONFIG
    config += ('\n[model_providers.validation]\nname = "Validation provider"\n'
               'env_key = "LOOM_VALIDATION_MODEL_API_KEY"\nwire_api = "responses"\nbase_url = '
               + json.dumps(value["base_url"]) + "\n")
    return config, value["api_key"].encode(), {k: value[k] for k in ("model", "base_url", "wire_api")}


def block_ssh(container: str, ports: list[int]) -> dict:
    """Never alter the host namespace, including with a remote Docker daemon."""
    details = json.loads(command("docker", "inspect", container))[0]
    pid = details["State"]["Pid"]
    namespace = Path(f"/proc/{pid}/ns/net")
    if not namespace.exists() or namespace.stat().st_ino == Path("/proc/self/ns/net").stat().st_ino:
        raise RuntimeError("Container network namespace is not locally isolated")
    # Container and host must refer to the same process, not a coincidentally reused remote PID.
    inside = command("docker", "exec", container, "readlink", "/proc/1/ns/net").decode().strip()
    if os.readlink(namespace) != inside:
        raise RuntimeError("Docker must run on this host")
    rules = {}
    for executable in ("iptables", "ip6tables"):
        for port in sorted(set(ports)):
            command("nsenter", "-t", str(pid), "-n", executable,
                    "-A", "OUTPUT", "-p", "tcp", "--dport", str(port), "-j", "REJECT")
        rules[executable] = command("nsenter", "-t", str(pid), "-n", executable,
                                    "-S", "OUTPUT").decode()
    return rules


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--image", default="loom-skill-validation:codex-0.164.0")
    parser.add_argument("--output", type=Path, required=True, help="New operator evidence directory")
    parser.add_argument("--prompt-file", type=Path)
    parser.add_argument("--observer")
    parser.add_argument("--authority", default="loom.local")
    parser.add_argument("--workspace", default="workspace-default")
    parser.add_argument("--grip-token-file", type=Path)
    authentication = parser.add_mutually_exclusive_group()
    authentication.add_argument("--codex-auth-file", type=Path)
    authentication.add_argument("--codex-provider-file", type=Path,
                                help="Private JSON containing only model/base_url/wire_api/api_key")
    parser.add_argument("--model", help="Optional explicitly selected model; otherwise use CLI default")
    parser.add_argument("--reasoning-effort", choices=("minimal", "low", "medium", "high", "xhigh", "max"),
                        help="Explicit Codex model reasoning effort")
    parser.add_argument("--ssh-port", type=int, action="append", default=[22])
    parser.add_argument("--preflight-only", action="store_true", help="No model or Observer calls")
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("Run as root on the test Docker host, not inside the agent")
    if any(p < 1 or p > 65535 for p in args.ssh_port):
        parser.error("Invalid SSH port")
    if not args.preflight_only and not all((args.prompt_file, args.observer, args.grip_token_file,
                                            args.codex_auth_file or args.codex_provider_file)):
        parser.error("Live execution requires prompt, Observer, GRIP token file and model authentication")
    for tool in ("docker", "nsenter", "iptables", "ip6tables"):
        if not shutil.which(tool):
            parser.error("Missing operator tool: " + tool)
    files = skill_files(args.bundle)
    base_config, provider_key, provider_metadata = BASE_CONFIG, None, None
    if args.codex_provider_file:
        base_config, provider_key, provider_metadata = provider_inputs(args.codex_provider_file)
        # The CLI talks to a local, operator-owned proxy. A session placeholder
        # satisfies its API-key interface without disclosing either real key.
        base_config = base_config.replace(json.dumps(provider_metadata["base_url"]), json.dumps("http://127.0.0.1:8889/model"))
    os.umask(0o077)
    output = args.output.resolve()
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    work = output / "work"
    work.mkdir(mode=0o700)
    os.chown(work, 1000, 1000)
    container = "loom-skill-eval-" + uuid.uuid4().hex[:12]
    network = container + "-net"
    summary = {"container": container, "status": "preparing", "started_at": int(time.time()),
               "bundle_sha256": hashlib.sha256(args.bundle.read_bytes()).hexdigest()}
    if provider_metadata:
        summary["model_provider"] = provider_metadata
    if args.model:
        summary["model"] = args.model
    if args.reasoning_effort:
        summary["reasoning_effort"] = args.reasoning_effort
    try:
        summary["stage"] = "image"
        summary["image_id"] = command("docker", "image", "inspect", args.image,
                                      "--format", "{{.Id}}").decode().strip()
        summary["stage"] = "isolated_network"
        summary["network_id"] = command("docker", "network", "create", "--driver", "bridge",
                                        network).decode().strip()
        command("docker", "run", "-d", "--name", container,
                "--user", "1000:1000", "--read-only", "--cap-drop=ALL",
                "--security-opt", "no-new-privileges:true", "--network", network,
                "--pids-limit", "256", "--memory", "2g", "--cpus", "2",
                "--tmpfs", "/home/agent:rw,nosuid,nodev,uid=1000,gid=1000,mode=700,size=256m",
                "--tmpfs", "/tmp:rw,nosuid,nodev,size=512m",
                "--tmpfs", "/run/loom-operator:rw,nosuid,nodev,uid=0,gid=0,mode=700,size=2m",
                "--mount", f"type=bind,src={work},dst=/work",
                "--env", "LOOM_OBSERVER_URL=" + ("http://127.0.0.1:8889" if provider_key and not args.preflight_only else args.observer or ""),
                "--env", "LOOM_GRIP_AUTHORITY=" + args.authority,
                "--env", "LOOM_WORKSPACE_ID=" + args.workspace,
                "--env", "LOOM_GRIP_TOKEN_FILE=/home/agent/.loom/grip-token",
                "--env", "PYTHONDONTWRITEBYTECODE=1", args.image)
        summary["stage"] = "ssh_network_isolation"
        summary["ssh_egress_rules"] = block_ssh(container, args.ssh_port)
        summary["stage"] = "filesystem_and_skills"
        summary["boundary"] = json.loads(command("docker", "exec", container,
                                                 "python", "-c", BOUNDARY_SCRIPT))
        files[".codex/config.toml"] = base_config.encode()
        put_files(container, files)
        summary["skills"] = isolate_skills(container, base_config)
        summary["codex_version"] = command("docker", "exec", container, "codex", "--version").decode().strip()
        (output / "preflight.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
        if args.preflight_only:
            summary["status"] = "preflight_passed"
        else:
            summary["stage"] = "authentication_inputs"
            token = args.grip_token_file.read_bytes().strip()
            if not token:
                raise ValueError("Empty authentication input")
            if provider_key:
                session_token = "evaluation-" + uuid.uuid4().hex
                start_credential_proxy(container, args.observer, token,
                                       json.loads(args.codex_provider_file.read_text()), session_token)
                token, provider_key = session_token.encode(), b"local-proxy-session"
                summary["boundary"]["operator_credentials_unreadable"] = True
                (output / "preflight.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
            authentication_files = {".loom/grip-token": token}
            if provider_key:
                authentication_files[".codex/provider-key"] = provider_key
            else:
                auth = json.loads(args.codex_auth_file.read_text())
                if not auth:
                    raise ValueError("Empty Codex authentication input")
                authentication_files[".codex/auth.json"] = json.dumps(auth).encode()
            put_files(container, authentication_files)
            prompt = args.prompt_file.read_bytes()
            (output / "prompt.txt").write_bytes(prompt)
            invocation = ["docker", "exec", "-i", container]
            if provider_key:
                invocation += ["python", "-c",
                               "import os, pathlib, sys; "
                               "os.environ['LOOM_VALIDATION_MODEL_API_KEY'] = pathlib.Path('/home/agent/.codex/provider-key').read_text(); "
                               "os.execvp('codex', sys.argv[1:])"]
            invocation += ["codex", "exec",
                          "--skip-git-repo-check", "--ephemeral", "--json",
                          "--color", "never", "-C", "/work", "-o", "/work/final.md"]
            if args.model:
                invocation += ["--model", args.model]
            if args.reasoning_effort:
                invocation += ["-c", "model_reasoning_effort=" + json.dumps(args.reasoning_effort)]
            invocation += ["-"]
            with (output / "events.jsonl").open("wb") as events, (output / "stderr.log").open("wb") as errors:
                summary["stage"] = "codex_execution"
                try:
                    # Do not impose a wall-clock budget on Codex. Remote task
                    # budgets belong to the submitted Run's
                    # execution_timeout_seconds. Codex may keep polling,
                    # collecting evidence and writing its report after that
                    # Run reaches a terminal state.
                    result = subprocess.run(invocation, input=prompt, stdout=events, stderr=errors,
                                            check=False)
                    summary.update(status="agent_finished" if result.returncode == 0 else "agent_failed",
                                   exit_code=result.returncode)
                except KeyboardInterrupt:
                    summary["status"] = "operator_interrupted"
            # CLI success is deliberately not a business acceptance result.
            summary["acceptance"] = "pending_independent_review"
    except Exception as exc:
        summary.update(status="setup_failed", error_type=type(exc).__name__)
        if isinstance(exc, (RuntimeError, ValueError)):
            summary["error"] = str(exc)
        # Avoid rendering subprocess args/output, which may contain credentials.
        raise RuntimeError("Validation setup failed; see local summary and preflight evidence") from None
    finally:
        subprocess.run(["docker", "rm", "-f", container], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        subprocess.run(["docker", "network", "rm", network], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        summary["ended_at"] = int(time.time())
        (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
        print(json.dumps({"output": str(output), "status": summary["status"]}))
    if summary["status"] not in {"preflight_passed", "agent_finished"}:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
