import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from loom_business_skills import Problem
from loom_business_skills import cli
from conftest import draft


async def test_cli_result_goes_to_private_external_file_not_cache(memory, client, store, tmp_path, monkeypatch):
    await memory.learn(draft(client))
    token = tmp_path / "token"
    token.write_text(client.token)
    arguments = tmp_path / "arguments.json"
    arguments.write_text('{"value":21}')
    result = tmp_path / "result.json"
    monkeypatch.setattr(cli, "LoomClient", lambda *a, **kw: client)
    args = cli.parser().parse_args(["--cache", str(store.root), "--observer", client.base_url,
                                  "--token-file", str(token), "run", "paper-review", "catalog",
                                  "--arguments", str(arguments), "--key", "one-call", "--result-file", str(result)])
    receipt = await cli.dispatch(args)
    assert receipt["acceptance_pending"]
    assert "RAW-BUSINESS" not in json.dumps(receipt)
    assert json.loads(result.read_text())["value"]["value"] == 42
    assert result.stat().st_mode & 0o077 == 0
    assert len(client.dispatches) == 1
    with pytest.raises(Problem, match="result_destination_invalid"):
        await cli.dispatch(args)
    assert len(client.dispatches) == 1
    args.result_file = store.root / "not-in-cache.json"
    with pytest.raises(Problem, match="result_destination_invalid"):
        await cli.dispatch(args)
    assert len(client.dispatches) == 1


async def test_second_process_loads_only_selected_experience(memory, client, store, tmp_path):
    await memory.learn(draft(client, key="paper-catalog"))
    other = draft(client, key="project-list")
    other.update(title="Project inventory", summary="Find current document projects", keywords=["projects", "latex"])
    await memory.learn(other)
    script = Path(__file__).resolve().parents[1] / "scripts/memory.py"
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    result = subprocess.run([sys.executable, str(script), "--cache", str(store.root), "find", "projects"],
                            cwd=tmp_path, env=env, capture_output=True, text=True, check=True)
    found = json.loads(result.stdout)
    assert [r["id"] for r in found["candidates"]] == ["project-list"]
    result = subprocess.run([sys.executable, str(script), "--cache", str(store.root), "history", "project-list"],
                            cwd=tmp_path, env=env, capture_output=True, text=True, check=True)
    data = json.loads(result.stdout)
    assert data["historical_only"] is True and data["id"] == "project-list"
    assert "paper-catalog" not in result.stdout


def test_plugin_package_contains_one_skill_and_no_learned_data(tmp_path):
    import importlib.util
    import zipfile
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("builder", root / "scripts/build_plugin.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sdk = tmp_path / "loom_orchestration-0.10.0-py3-none-any.whl"
    with zipfile.ZipFile(sdk, "w") as z:
        z.writestr("loom_orchestration-0.10.0.dist-info/METADATA", "Name: loom-orchestration\nVersion: 0.10.0\n")
    archive = module.build(tmp_path / "plugin.zip", sdk)
    with zipfile.ZipFile(archive) as z:
        files = z.namelist()
        assert sum(n.startswith("skills/") and n.endswith("/SKILL.md") for n in files) == 1
        assert "plugin.json" in files
        assert not any(n.endswith(".sqlite3") or n.startswith(("experiences/", "loom_v2/")) for n in files)
