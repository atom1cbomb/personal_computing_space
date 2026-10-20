import json
from pathlib import Path

from loom_v2.deployment.compose import render_project
from .test_config import config_text, write_secret
from loom_v2.deployment.config import DeploymentConfig


def make_config(tmp_path: Path) -> DeploymentConfig:
    for filename, value in {
        "internal.secret": "internal-token",
        "postgres.secret": "pg-password",
        "minio.secret": "minio-password",
    }.items():
        write_secret(tmp_path / filename, value)
    path = tmp_path / "deployment.toml"
    path.write_text(config_text(), encoding="utf-8")
    return DeploymentConfig.from_file(path)


def test_observer_project_contains_private_postgres_and_public_port(tmp_path: Path):
    project = render_project(make_config(tmp_path), "observer")
    document = json.loads(project.compose_text)

    assert set(document["services"]) == {"observer", "postgres"}
    assert document["services"]["observer"]["ports"] == ["8080:8080"]
    assert document["services"]["observer"]["environment"]["LOOM_DATABASE_URL"].startswith(
        "postgresql+asyncpg://loom:"
    )
    assert document["services"]["postgres"]["volumes"] == ["observer-postgres-data:/var/lib/postgresql/data"]
    assert "POSTGRES_PASSWORD='pg-password'" in project.env_text
    assert project.secret_files["internal_api_secret"] == "internal-token"


def test_slave_project_uses_observer_url_and_isolated_database(tmp_path: Path):
    project = render_project(make_config(tmp_path), "worker-a")
    document = json.loads(project.compose_text)

    assert set(document["services"]) == {"slave", "postgres"}
    assert document["services"]["slave"]["environment"]["LOOM_OBSERVER_URL"] == "http://10.0.0.11:8080"
    assert document["services"]["postgres"]["volumes"] == ["worker-a-postgres-data:/var/lib/postgresql/data"]
    slave = document["services"]["slave"]
    state_dir = slave["environment"]["LOOM_RUNTIME_PLUGIN_INSTANCE_STATE_DIR"]
    assert state_dir == "/var/lib/loom/runtime-plugin-state"
    assert f"{state_dir}:{state_dir}" in slave["volumes"]


def test_driver_project_uses_observer_and_docker_socket(tmp_path: Path):
    project = render_project(make_config(tmp_path), "driver")
    document = json.loads(project.compose_text)
    environment = document["services"]["driver"]["environment"]

    assert environment["LOOM_OBSERVER_URL"] == "http://10.0.0.11:8080"
    assert environment["LOOM_DRIVER_URL"] == "http://10.0.0.12:8090"
    assert "LOOM_SLAVE_A_URL" not in environment
    assert "LOOM_SLAVE_B_URL" not in environment
    assert environment["LOOM_S3_ENDPOINT_URL"] == "${LOOM_S3_ENDPOINT_URL}"
    assert "args" not in document["services"]["driver"]["build"]
    assert document["services"]["driver"]["volumes"][-1] == "/var/run/docker.sock:/var/run/docker.sock"
    assert "pg-password" not in project.redacted_preview
    assert "internal-token" not in project.redacted_preview


def test_driver_project_ignores_legacy_codex_provider_secret(tmp_path: Path):
    for filename, value in {
        "internal.secret": "internal-token",
        "postgres.secret": "pg-password",
        "minio.secret": "minio-password",
    }.items():
        write_secret(tmp_path / filename, value)
    write_secret(tmp_path / "ark.secret", "ark-key")
    path = tmp_path / "deployment.toml"
    path.write_text(
        config_text().replace(
            'codex_base_url = "http://codex.internal:8787"',
            'codex_provider = "volcengine-agent-plan"\n'
            'codex_base_url = "https://ark.cn-beijing.volces.com/api/plan/v3"\n'
            'codex_api_key_env = "ARK_API_KEY"\n'
            'codex_api_key_file = "ark.secret"',
        ),
        encoding="utf-8",
    )
    project = render_project(DeploymentConfig.from_file(path), "driver")
    document = json.loads(project.compose_text)
    driver = document["services"]["driver"]

    assert not any(key.startswith("LOOM_CODEX_") for key in driver["environment"])
    assert "codex_api_key" not in driver["secrets"]
    assert "codex_api_key" not in document["secrets"]
    assert "codex_api_key" not in project.secret_files
    assert "ark-key" not in project.redacted_preview


def test_minio_project_contains_bucket_initializer(tmp_path: Path):
    project = render_project(make_config(tmp_path), "storage")
    document = json.loads(project.compose_text)

    assert set(document["services"]) == {"minio", "minio-init"}
    assert document["services"]["minio"]["ports"] == ["9000:9000", "9001:9001"]
    assert isinstance(document["services"]["minio-init"]["command"], list)
    assert "mc mb --ignore-existing" in document["services"]["minio-init"]["command"][0]


def test_slave_advertises_cross_host_endpoint_to_observer(tmp_path: Path):
    project = render_project(make_config(tmp_path), "worker-a")
    environment = json.loads(project.compose_text)["services"]["slave"]["environment"]

    assert environment["LOOM_SLAVE_ENDPOINT_URL"] == "http://10.0.0.13:8081"


def test_slave_mounts_operator_runtime_credentials_when_configured(tmp_path: Path):
    path = tmp_path / "deployment.toml"
    for filename, value in {
        "internal.secret": "internal-token",
        "postgres.secret": "pg-password",
        "minio.secret": "minio-password",
    }.items():
        write_secret(tmp_path / filename, value)
    path.write_text(
        config_text().replace(
            "workspace_id = \"workspace-default\"",
            "workspace_id = \"workspace-default\"\n"
            "runtime_credentials_dir = \"/var/lib/loom/runtime-credentials\"\n"
            "runtime_plugin_state_dir = \"/srv/loom/instance-state\"\n"
            "runtime_plugin_egress_network = \"loom-egress\"",
        ),
        encoding="utf-8",
    )
    project = render_project(DeploymentConfig.from_file(path), "worker-a")
    app = json.loads(project.compose_text)["services"]["slave"]

    assert app["environment"]["LOOM_RUNTIME_PLUGIN_CREDENTIAL_DIR"] == "/var/lib/loom/runtime-credentials"
    assert app["environment"]["LOOM_RUNTIME_PLUGIN_EGRESS_NETWORK"] == "loom-egress"
    assert app["environment"]["LOOM_RUNTIME_PLUGIN_INSTANCE_STATE_DIR"] == "/srv/loom/instance-state"
    assert "/srv/loom/instance-state:/srv/loom/instance-state" in app["volumes"]
    assert "/var/lib/loom/runtime-credentials:/var/lib/loom/runtime-credentials:ro" in app["volumes"]


def test_database_url_uses_url_encoded_password_variable(tmp_path: Path):
    for filename, value in {
        "internal.secret": "internal-token",
        "postgres.secret": "p@ss/word",
        "minio.secret": "minio-password",
    }.items():
        write_secret(tmp_path / filename, value)
    path = tmp_path / "deployment.toml"
    path.write_text(config_text(), encoding="utf-8")
    project = render_project(DeploymentConfig.from_file(path), "observer")
    document = json.loads(project.compose_text)

    assert document["services"]["observer"]["environment"]["LOOM_DATABASE_URL"] == "postgresql+asyncpg://loom:${POSTGRES_PASSWORD_URLENCODED}@postgres:5432/loom_observer"
    assert "POSTGRES_PASSWORD_URLENCODED='p%40ss%2Fword'" in project.env_text
    assert "p%40ss%2Fword" not in project.redacted_preview


def test_grip_configuration_is_delivered_only_to_observer_and_redacted(tmp_path):
    from dataclasses import replace
    config = replace(make_config(tmp_path), grip_authority="resources.example", public_base_url="https://observer.example", grip_token="private-grip-fixture")
    observer = render_project(config, "observer")
    document = json.loads(observer.compose_text)
    assert document["services"]["observer"]["environment"]["LOOM_GRIP_AUTHORITY"] == "resources.example"
    assert document["services"]["observer"]["environment"]["LOOM_PUBLIC_BASE_URL"] == "https://observer.example"
    assert document["secrets"]["grip_token"]["file"] == "./secrets/grip_token"
    assert "cat /run/secrets/grip_token" in document["services"]["observer"]["command"][2]
    assert observer.secret_files["grip_token"] == "private-grip-fixture"
    assert "private-grip-fixture" not in observer.redacted_preview
    assert "grip_token" not in render_project(config, "worker-a").secret_files
