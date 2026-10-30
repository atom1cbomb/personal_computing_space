"""Build matching skill, standalone SDK wheel and Observer download manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import tomllib


def build_bundle(output: Path, *, source_root: Path | None = None) -> Path:
    root = source_root or Path(__file__).resolve().parents[1]
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="loom-skill-") as temporary:
        staging = Path(temporary)
        skill = staging / "personal-computing-space"
        shutil.copytree(root / "skills" / skill.name, skill, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        wheels = skill / "assets" / "wheels"
        # Always build the wheel from this release, even if the input skill was
        # unpacked from an earlier archive containing a bundled SDK.
        shutil.rmtree(wheels, ignore_errors=True)
        wheels.mkdir(parents=True)
        source = staging / "source"
        package = source / "packages" / "loom-orchestration"
        package.mkdir(parents=True)
        shutil.copy2(root / "packages/loom-orchestration/pyproject.toml", package / "pyproject.toml")
        shutil.copytree(root / "loom_orchestration", source / "loom_orchestration", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        subprocess.run([
            sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation",
            "--wheel-dir", str(wheels), str(package),
        ], check=True)
        wheel, = wheels.glob("loom_orchestration-*.whl")
        archive = Path(shutil.make_archive(
            str(staging / "personal-computing-space"), "zip", staging, skill.name,
        ))
        def artifact(path: Path) -> dict:
            return {"filename": path.name, "size": path.stat().st_size,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}

        version = tomllib.loads((package / "pyproject.toml").read_text())["project"]["version"]
        manifest = {"skill": artifact(archive), "sdk": {**artifact(wheel), "version": version}}
        # Build all artifacts before replacing an earlier release. Publish the
        # manifest last; production serves these from the immutable image.
        for path in (archive, wheel):
            temporary = output / (path.name + ".tmp")
            shutil.copyfile(path, temporary)
            temporary.replace(output / path.name)
        for old in output.glob("loom_orchestration-*.whl"):
            if old.name != wheel.name:
                old.unlink()
        temporary = output / "manifest.json.tmp"
        temporary.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        temporary.replace(output / "manifest.json")
    return output / archive.name


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("dist"))
    arguments = parser.parse_args()
    print(build_bundle(arguments.output))


if __name__ == "__main__":
    main()
