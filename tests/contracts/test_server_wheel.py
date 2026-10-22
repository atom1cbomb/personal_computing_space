import subprocess
import sys
import zipfile
from pathlib import Path


def test_server_wheel_contains_runtime_assets(tmp_path):
    root = Path(__file__).resolve().parents[2]
    subprocess.run([sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation", "--wheel-dir", str(tmp_path), str(root)], check=True, capture_output=True)
    wheel, = tmp_path.glob("loom_v2-*.whl")
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
    assert "loom_v2/web/static/index.html" in names
    assert "loom_orchestration/metadata.py" in names
    assert "migrations/alembic/env.py" in names
    assert "migrations/alembic/versions/004_run_execution_requests.py" in names
    assert "migrations/alembic/versions/005_slave_action_name.py" in names
    assert "migrations/alembic/versions/006_mcue_attributes.py" in names
    assert "migrations/alembic/versions/007_activation_credentials.py" in names
    assert "loom_v2/mcue_types/computing-vm-v1.json" in names
    assert "loom_v2/mcue_types/computing-volcano-v1.json" in names
    assert "loom_v2/mcue_types/computing-grafana-v1.json" in names
    assert "loom_orchestration/mcue.py" in names
