"""A new build replaces both skill content and the standalone SDK release."""
import hashlib
import json
from pathlib import Path
import re
import zipfile

from scripts.build_agent_skill import build_bundle


def test_rebuild_updates_skill_sdk_manifest_and_removes_old_wheels(tmp_path):
    root = tmp_path / "source"
    skill = root / "skills/personal-computing-space"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("first skill")
    old_wheels = skill / "assets/wheels"
    old_wheels.mkdir(parents=True)
    (old_wheels / "loom_orchestration-0.0.0-py3-none-any.whl").write_bytes(b"stale bundled wheel")
    package = root / "packages/loom-orchestration"
    package.mkdir(parents=True)
    config = Path(__file__).resolve().parents[2] / "packages/loom-orchestration/pyproject.toml"
    (package / "pyproject.toml").write_text(re.sub(r'^version = "[^"]+"', 'version = "1.0.0"', config.read_text(), flags=re.MULTILINE))
    sdk = root / "loom_orchestration"
    sdk.mkdir()
    (sdk / "__init__.py").write_text('RELEASE = "first"\n')
    output = tmp_path / "release"
    build_bundle(output, source_root=root)
    first = json.loads((output / "manifest.json").read_text())
    assert first["sdk"]["version"] == "1.0.0"
    with zipfile.ZipFile(output / first["skill"]["filename"]) as archive:
        assert len([n for n in archive.namelist() if n.endswith(".whl")]) == 1

    (skill / "SKILL.md").write_text("second skill")
    (sdk / "__init__.py").write_text('RELEASE = "second"\n')
    (package / "pyproject.toml").write_text((package / "pyproject.toml").read_text().replace('version = "1.0.0"', 'version = "1.1.0"'))
    build_bundle(output, source_root=root)
    second = json.loads((output / "manifest.json").read_text())
    assert second["sdk"]["version"] == "1.1.0"
    assert not (output / first["sdk"]["filename"]).exists()
    with zipfile.ZipFile(output / second["skill"]["filename"]) as archive:
        assert archive.read("personal-computing-space/SKILL.md") == b"second skill"
        wheels = [n for n in archive.namelist() if n.endswith(".whl")]
        assert len(wheels) == 1
        assert archive.read(wheels[0]) == (output / second["sdk"]["filename"]).read_bytes()
    with zipfile.ZipFile(output / second["sdk"]["filename"]) as archive:
        assert archive.read("loom_orchestration/__init__.py") == b'RELEASE = "second"\n'
    for key in ("skill", "sdk"):
        artifact = second[key]
        assert artifact["sha256"] != first[key]["sha256"]
        assert hashlib.sha256((output / artifact["filename"]).read_bytes()).hexdigest() == artifact["sha256"]
