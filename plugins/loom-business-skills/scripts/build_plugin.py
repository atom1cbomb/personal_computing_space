"""Build a plugin ZIP with exactly one discoverable skill and the unchanged public SDK."""
import argparse
from pathlib import Path
import zipfile


def build(output, sdk_wheel):
    root = Path(__file__).resolve().parents[1]
    if len(list((root / "skills").rglob("SKILL.md"))) != 1:
        raise ValueError("exactly_one_entry_skill_required")
    with zipfile.ZipFile(sdk_wheel) as sdk:
        metadata = next(n for n in sdk.namelist() if n.endswith('.dist-info/METADATA'))
        contents = sdk.read(metadata).decode()
        if 'Name: loom-orchestration\n' not in contents or 'Version: 0.10.' not in contents:
            raise ValueError('matching_public_sdk_wheel_required')
        if any(n.startswith('loom_v2/') for n in sdk.namelist()):
            raise ValueError('server_code_in_sdk')
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as z:
        for name in ("plugin.json", "pyproject.toml", "README.md", "DESIGN.md"):
            z.write(root / name, name)
        for folder in ("src", "scripts", "skills", "references"):
            for path in sorted((root / folder).rglob("*")):
                if (path.is_file() and '__pycache__' not in path.parts and path.suffix != '.pyc'
                        and not any(part.endswith('.egg-info') for part in path.parts)):
                    z.write(path, path.relative_to(root))
        z.write(sdk_wheel, 'assets/wheels/' + sdk_wheel.name)
    return output


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--sdk-wheel", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    print(build(a.output, a.sdk_wheel))
