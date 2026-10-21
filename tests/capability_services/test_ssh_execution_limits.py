import importlib
import json
import os
from pathlib import Path
import shlex
import sys

import pytest

from capability_services import ssh_execution
from loom_v2.contracts.io_schema import validate, validate_schema


@pytest.fixture(params=["vm_ssh_http", "suanli_vm_ssh_http"])
def gateway(request, tmp_path):
    module = importlib.import_module(f"capability_services.{request.param}.server")
    instance = tmp_path / "instance.json"
    instance.write_text(json.dumps({"host": "192.0.2.7", "port": 22, "user": "root"}))
    credentials = tmp_path / "credentials"
    credentials.mkdir()
    (credentials / "id_ed25519").touch()
    ssh = tmp_path / "ssh"
    ssh.write_text('#!/bin/sh\nexec /bin/sh\n')
    ssh.chmod(0o755)
    kwargs = {"credential_root": credentials} if request.param == "vm_ssh_http" else {}
    return module.VmSshGateway(instance_path=instance, state_root=tmp_path / "state", ssh=str(ssh), **kwargs)


def python_command(source):
    return f"{shlex.quote(sys.executable)} -c {shlex.quote(source)}"


def test_full_length_unicode_script_is_sent_through_stdin(gateway):
    suffix = '\nprintf "script finished"'
    command = "#" + "汉" * (ssh_execution.MAX_COMMAND - len(suffix) - 1) + suffix
    result = gateway.exec(command, 600)
    assert result == {"stdout": "script finished", "stderr": "", "exit_code": 0, "timed_out": False, "truncated": False}


def test_output_above_old_limit_is_preserved_on_both_streams(gateway):
    size = 5 * 1024 * 1024
    result = gateway.exec(python_command(f'import sys; sys.stdout.write("a"*{size}); sys.stderr.write("b"*{size})'))
    assert result["stdout"] == "a" * size
    assert result["stderr"] == "b" * size
    assert result["exit_code"] == 0 and not result["truncated"]


def test_output_is_drained_after_truncation(gateway, monkeypatch):
    monkeypatch.setattr(ssh_execution, "MAX_OUTPUT", 64 * 1024)
    result = gateway.exec(python_command('import sys; sys.stdout.write("a"*2097152); sys.stderr.write("b"*2097152)'))
    assert result["stdout"] == "a" * 65536
    assert result["stderr"] == "b" * 65536
    assert result["truncated"] and result["exit_code"] == 0


def test_timeout_kills_local_process_group_and_preserves_output(gateway):
    result = gateway.exec('echo "$$"; sleep 30', 1)
    assert result["timed_out"] and result["exit_code"] is None
    with pytest.raises(ProcessLookupError):
        os.kill(int(result["stdout"].strip()), 0)


def test_exit_code_is_preserved(gateway):
    result = gateway.exec('printf failure >&2; exit 7')
    assert result["exit_code"] == 7 and not result["timed_out"]
    assert result["stderr"] == "failure"


def test_default_and_explicit_timeouts_are_not_silently_shortened(gateway, monkeypatch):
    module = importlib.import_module(type(gateway).__module__)
    seen = []
    monkeypatch.setattr(module, "execute_ssh", lambda args, command, timeout: seen.append(timeout) or {})
    gateway.handle('/v1/exec', {"command": "id"})
    gateway.exec("id", 3600)
    assert seen == [300.0, 3600.0]


@pytest.mark.parametrize("timeout", [True, "300", -1, 0.5, 3601, float('nan'), float('inf')])
def test_invalid_timeout_is_rejected(gateway, timeout):
    with pytest.raises(ssh_execution.GatewayError, match="timeout_invalid"):
        gateway.exec("id", timeout)


@pytest.mark.parametrize("command", ["", " \n", "x\x00y", "x" * (ssh_execution.MAX_COMMAND + 1)])
def test_invalid_command_is_rejected(gateway, command):
    with pytest.raises(ssh_execution.GatewayError, match="command_invalid"):
        gateway.exec(command)


@pytest.mark.parametrize("package", ["vm_ssh_http", "suanli_vm_ssh_http"])
def test_input_schema_exposes_length_and_timeout_limits(package):
    path = Path(__file__).parents[2] / 'capability_services' / package / 'schemas/exec-input.json'
    schema = json.loads(path.read_text())
    validate_schema(schema)
    assert not validate(schema, {"command": "x" * ssh_execution.MAX_COMMAND, "timeout_seconds": 3600})
    assert validate(schema, {"command": "x" * (ssh_execution.MAX_COMMAND + 1)})
    assert validate(schema, {"command": "id", "timeout_seconds": 3601})
