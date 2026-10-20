"""Build a portable skill archive containing the public SDK wheel."""

from __future__ import annotations

import argparse
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


def build_bundle(output: Path) -> Path:
    root = Path(__file__).resolve().parents[1]
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="loom-skill-") as temporary:
        staging = Path(temporary)
        skill = staging / "personal-computing-space"
        shutil.copytree(root / "skills" / skill.name, skill, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        wheels = skill / "assets" / "wheels"
        wheels.mkdir(parents=True, exist_ok=True)
        source = staging / "source"
        package = source / "packages" / "loom-orchestration"
        package.mkdir(parents=True)
        shutil.copy2(root / "packages/loom-orchestration/pyproject.toml", package / "pyproject.toml")
        shutil.copytree(root / "loom_orchestration", source / "loom_orchestration", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        subprocess.run([
            sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation",
            "--wheel-dir", str(wheels), str(package),
        ], check=True)
        archive = shutil.make_archive(
            str(output.resolve() / "personal-computing-space"), "zip", staging, skill.name,
        )
    return Path(archive)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("dist"))
    arguments = parser.parse_args()
    print(build_bundle(arguments.output))


if __name__ == "__main__":
    main()
