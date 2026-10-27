from __future__ import annotations

import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import tomllib
import zipfile

import pytest


SPEC = importlib.util.spec_from_file_location(
    "run_skill_validation", Path(__file__).resolve().parents[2] / "scripts/run_skill_validation.py"
)
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


def bundle(tmp_path, *, extra=None, server_module=False, include_wheel=True):
    wheel = io.BytesIO()
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("loom_orchestration/__init__.py", "")
        if server_module:
            archive.writestr("loom_v2/__init__.py", "")
    path = tmp_path / "skill.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("personal-computing-space/SKILL.md", "# Test skill")
        if include_wheel:
            archive.writestr(
                "personal-computing-space/assets/wheels/loom_orchestration-0.8.0-py3-none-any.whl",
                wheel.getvalue(),
            )
        if extra:
            archive.writestr(extra, "forbidden")
    return path


def test_release_bundle_is_the_only_project_input(tmp_path):
    files = runner.skill_files(bundle(tmp_path))
    assert set(files) == {
        ".agents/skills/personal-computing-space/SKILL.md",
        ".agents/skills/personal-computing-space/assets/wheels/loom_orchestration-0.8.0-py3-none-any.whl",
    }


@pytest.mark.parametrize("entry", [
    "personal-computing-space/../../.ssh/id_rsa",
    "/etc/profile",
    "other-skill/SKILL.md",
])
def test_rejects_archive_escape_or_extra_skill(tmp_path, entry):
    with pytest.raises(ValueError, match="Unexpected"):
        runner.skill_files(bundle(tmp_path, extra=entry))


def test_rejects_archive_symlink(tmp_path):
    link = zipfile.ZipInfo("personal-computing-space/references/host")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    with pytest.raises(ValueError, match="Unexpected"):
        runner.skill_files(bundle(tmp_path, extra=link))


def test_rejects_server_source_in_sdk(tmp_path):
    with pytest.raises(ValueError, match="Server modules"):
        runner.skill_files(bundle(tmp_path, server_module=True))


def test_requires_distributed_sdk_not_a_source_skill(tmp_path):
    with pytest.raises(ValueError, match="SDK wheel"):
        runner.skill_files(bundle(tmp_path, include_wheel=False))


def test_host_network_is_rejected_before_any_firewall_write(monkeypatch):
    calls = []

    def fake_command(*args, **kwargs):
        calls.append(args)
        return json.dumps([{"State": {"Pid": os.getpid()}}]).encode()

    monkeypatch.setattr(runner, "command", fake_command)
    with pytest.raises(RuntimeError, match="not locally isolated"):
        runner.block_ssh("test-container", [22])
    assert calls == [("docker", "inspect", "test-container")]


def test_custom_provider_keeps_secret_out_of_config_and_metadata(tmp_path):
    profile = {"model": "test-model", "base_url": "https://model.example/v1",
               "wire_api": "responses", "api_key": "unit-test-credential"}
    path = tmp_path / "provider.json"
    path.write_text(json.dumps(profile))
    text, key, metadata = runner.provider_inputs(path)
    config = tomllib.loads(text)
    assert key == b"unit-test-credential"
    assert profile["api_key"] not in text and "api_key" not in metadata
    assert config["model"] == "test-model"
    assert config["features"]["plugins"] is False
    assert config["model_providers"]["validation"]["env_key"] in config["shell_environment_policy"]["exclude"]


@pytest.mark.parametrize("override", [
    {"mcp_servers": {"extra": {}}},
    {"base_url": "https://model.example/v1?token=private"},
])
def test_custom_provider_cannot_import_extra_context_or_url_credentials(tmp_path, override):
    profile = {"model": "test-model", "base_url": "https://model.example/v1",
               "wire_api": "responses", "api_key": "unit-test-credential", **override}
    path = tmp_path / "provider.json"
    path.write_text(json.dumps(profile))
    with pytest.raises(ValueError):
        runner.provider_inputs(path)
