from __future__ import annotations

import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _compose_config() -> dict:
    completed = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            "deploy/docker-compose.yml",
            "-f",
            "deploy/docker-compose.heterogeneous.yml",
            "config",
            "--format",
            "json",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(completed.stdout)


def test_heterogeneous_compose_has_three_isolated_slaves():
    config = _compose_config()
    services = config["services"]

    assert services["slave-a"]["cpus"] == 0.5
    assert services["slave-b"]["cpus"] == 1.5
    assert services["slave-c"]["cpus"] == 3.0
    assert services["slave-c"]["environment"]["LOOM_DATABASE_URL"].endswith(
        "@slave-c-db:5432/loom_slave_c"
    )
    assert services["slave-c"]["environment"]["LOOM_SLAVE_ENDPOINT_URL"] == "http://slave-c:8083"
    assert "slave-c-db" in services["slave-c"]["depends_on"]
    assert any(
        volume.get("source") == "slave-c-runtime-plugin-sockets"
        for volume in services["slave-c"]["volumes"]
    )


def test_each_slave_registers_distinct_per_package_profiles():
    services = _compose_config()["services"]
    profiles = {
        slave_id: json.loads(services[slave_id]["environment"]["LOOM_SCHEDULING_PROFILES"])
        for slave_id in ("slave-a", "slave-b", "slave-c")
    }

    assert [profiles[slave]["packages"]["yolo11n-http"]["service_time_ms"] for slave in profiles] == [
        1600,
        700,
        300,
    ]
    assert all(
        set(profile["packages"])
        == {
            "image-preprocess-http",
            "yolo11n-http",
            "inspection-assess-risk",
            "inspection-aggregate",
        }
        for profile in profiles.values()
    )
