from pathlib import Path

import pytest

from loom_v2.deployment.config import DeploymentConfig, DeploymentConfigError


def write_secret(path: Path, value: str) -> None:
    path.write_text(value, encoding="utf-8")


def config_text() -> str:
    return """
[cluster]
name = "loom-prod"
remote_dir = "/opt/loom-v2"
workspace_id = "workspace-default"
internal_secret_file = "internal.secret"
postgres_password_file = "postgres.secret"
minio_secret_key_file = "minio.secret"

[driver]
workspace_path = "/srv/loom/workspace"

[[machines]]
name = "storage"
ssh_host = "10.0.0.10"
ssh_user = "ubuntu"
role = "minio"

[[machines]]
name = "observer"
ssh_host = "10.0.0.11"
ssh_user = "ubuntu"
role = "observer"

[[machines]]
name = "driver"
ssh_host = "10.0.0.12"
ssh_user = "ubuntu"
role = "driver"

[[machines]]
name = "worker-a"
ssh_host = "10.0.0.13"
ssh_user = "ubuntu"
role = "slave"
service_id = "slave-a"
"""


def test_loads_defaults_and_derives_cross_host_urls(tmp_path: Path):
    for filename, value in {
        "internal.secret": "internal-token",
        "postgres.secret": "pg-password",
        "minio.secret": "minio-password",
    }.items():
        write_secret(tmp_path / filename, value)
    path = tmp_path / "deployment.toml"
    path.write_text(config_text(), encoding="utf-8")

    config = DeploymentConfig.from_file(path)

    assert config.machine("observer").service_port == 8080
    assert config.machine("worker-a").service_port == 8081
    assert config.urls.observer == "http://10.0.0.11:8080"
    assert config.urls.slaves == {"slave-a": "http://10.0.0.13:8081"}
    assert config.postgres_password == "pg-password"
    assert config.driver.workspace_path == "/srv/loom/workspace"


def test_inventory_accepts_third_slave_and_rejects_consumer_port_collision(tmp_path):
    for filename in ("internal.secret", "postgres.secret", "minio.secret"):
        write_secret(tmp_path / filename, "fixture-secret")
    text = config_text() + '''
[[machines]]
name = "worker-b"
ssh_host = "10.0.0.14"
ssh_user = "root"
role = "slave"
service_id = "slave-b"

[[machines]]
name = "autopaper"
ssh_host = "10.0.0.15"
ssh_user = "root"
role = "slave"
service_id = "slave-autopaper"
grip_data_port = 8091
[machines.consumer_ports."grip://loom.local/workspaces/workspace-default/packages/embedding-http/v1:autopapers"]
host_port = 8001
container_port = 8081
'''
    path = tmp_path / "deployment.toml"
    path.write_text(text)
    config = DeploymentConfig.from_file(path)
    assert len(config.slaves) == 3
    assert "slave-autopaper" in config.urls.slaves
    path.write_text(text.replace("host_port = 8001", "host_port = 8091"))
    with pytest.raises(DeploymentConfigError, match="published ports must be distinct"):
        DeploymentConfig.from_file(path)


def test_service_signing_key_requires_matching_public_key(tmp_path):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    for filename in ("internal.secret", "postgres.secret", "minio.secret"):
        write_secret(tmp_path / filename, "fixture-secret")
    key = Ed25519PrivateKey.generate()
    (tmp_path / "sign.pem").write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    (tmp_path / "verify.pem").write_bytes(key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    path = tmp_path / "deployment.toml"
    path.write_text(config_text().replace("[driver]", '''[cluster.service_access]
signing_key_file = "sign.pem"
verification_keys = { service-access-v1 = "verify.pem" }

[driver]'''))
    config = DeploymentConfig.from_file(path)
    assert set(config.service_key_material) == {"grip-signing", "grip-verify-service-access-v1"}
    assert "PRIVATE KEY" not in repr(config)
    (tmp_path / "verify.pem").write_bytes(Ed25519PrivateKey.generate().public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    with pytest.raises(DeploymentConfigError, match="do not match"):
        DeploymentConfig.from_file(path)


@pytest.mark.parametrize("mapping", ["data_bindings", "upstream_bindings"])
def test_service_node_prerequisites_are_validated_before_remote_deployment(mapping):
    import tomllib
    from pydantic import ValidationError
    from loom_v2.deployment.config import DeploymentInput

    raw = tomllib.loads(config_text())
    slave = "grip://loom.local/workspaces/workspace-default/slaves/slave-a"
    access = {mapping: {slave: [{"protocol": "https-json.v1", "endpoint": "https://node.example/.well-known/grip", "audience": slave}]}}
    raw["cluster"]["service_access"] = access
    with pytest.raises(ValidationError, match="require signing"):
        DeploymentInput.model_validate(raw)
    access.update(signing_key_file="sign.pem", verification_keys={"service-access-v1": "verify.pem"})
    with pytest.raises(ValidationError, match="slave-a require grip_data_port"):
        DeploymentInput.model_validate(raw)
    raw["machines"][-1]["grip_data_port"] = 8091
    assert DeploymentInput.model_validate(raw).machines[-1].grip_data_port == 8091
    access[mapping][slave][0]["audience"] = "grip://loom.local/workspaces/workspace-default"
    with pytest.raises(ValidationError, match="must not be the Observer"):
        DeploymentInput.model_validate(raw)


def test_insecure_service_binding_has_a_sanitized_configuration_error(tmp_path):
    path = tmp_path / "deployment.toml"
    path.write_text(config_text().replace("[driver]", '''[cluster.service_access.data_bindings]
"grip://loom.local/workspaces/workspace-default/slaves/slave-a" = [
  { protocol = "https-json.v1", endpoint = "http://private.example/path", audience = "grip://loom.local/workspaces/workspace-default/slaves/slave-a" }
]
[driver]'''))
    with pytest.raises(DeploymentConfigError, match="invalid service binding: unsupported_binding") as caught:
        DeploymentConfig.from_file(path)
    assert "private.example" not in str(caught.value)


@pytest.mark.parametrize("field,value", [
    ("codex_provider", "volcengine-agent-plan"),
    ("codex_base_url", "https://provider.example"),
    ("codex_api_key_file", "missing-api-key"),
    ("model_catalog_file", "missing-model-catalog"),
])
def test_driver_rejects_removed_model_configuration(tmp_path: Path, field, value):
    for filename in ("internal.secret", "postgres.secret", "minio.secret"):
        write_secret(tmp_path / filename, "fixture-secret")
    path = tmp_path / "deployment.toml"
    path.write_text(config_text().replace("[driver]", f'[driver]\n{field} = "{value}"'))
    with pytest.raises(DeploymentConfigError, match="Extra inputs are not permitted"):
        DeploymentConfig.from_file(path)


def test_rejects_missing_driver_or_duplicate_slave_id(tmp_path: Path):
    for filename in ("internal.secret", "postgres.secret", "minio.secret"):
        write_secret(tmp_path / filename, "secret")
    text = config_text().replace('role = "driver"', 'role = "observer"', 1)
    path = tmp_path / "deployment.toml"
    path.write_text(text, encoding="utf-8")

    with pytest.raises(DeploymentConfigError, match="exactly one driver"):
        DeploymentConfig.from_file(path)


def test_rejects_unreadable_or_empty_secret(tmp_path: Path):
    (tmp_path / "internal.secret").write_text("\n", encoding="utf-8")
    (tmp_path / "postgres.secret").write_text("pg", encoding="utf-8")
    (tmp_path / "minio.secret").write_text("minio", encoding="utf-8")
    path = tmp_path / "deployment.toml"
    path.write_text(config_text(), encoding="utf-8")

    with pytest.raises(DeploymentConfigError, match="internal_secret_file"):
        DeploymentConfig.from_file(path)


def test_all_test_roles_can_share_one_ssh_host_when_ports_are_distinct(tmp_path: Path):
    for filename in ("internal.secret", "postgres.secret", "minio.secret"):
        write_secret(tmp_path / filename, "secret")
    text = config_text()
    text += '''

[[machines]]
name = "worker-b"
ssh_host = "9.0.3.9"
ssh_user = "root"
ssh_port = 22
role = "slave"
service_id = "slave-b"
service_port = 18082
'''
    text = text.replace('ssh_host = "10.0.0.10"', 'ssh_host = "9.0.3.9"').replace('ssh_host = "10.0.0.11"', 'ssh_host = "9.0.3.9"').replace('ssh_host = "10.0.0.12"', 'ssh_host = "9.0.3.9"').replace('ssh_host = "10.0.0.13"', 'ssh_host = "9.0.3.9"')
    path = tmp_path / "deployment.toml"
    path.write_text(text, encoding="utf-8")

    config = DeploymentConfig.from_file(path)

    assert {machine.ssh_host for machine in config.machines} == {"9.0.3.9"}
    assert {machine.ssh_port for machine in config.machines} == {22}
    assert config.urls.slaves["slave-b"] == "http://9.0.3.9:18082"


def test_rejects_relative_driver_workspace_path(tmp_path: Path):
    for filename in ("internal.secret", "postgres.secret", "minio.secret"):
        write_secret(tmp_path / filename, "secret")
    path = tmp_path / "deployment.toml"
    path.write_text(config_text().replace('workspace_path = "/srv/loom/workspace"', 'workspace_path = "relative/workspace"'), encoding="utf-8")

    with pytest.raises(DeploymentConfigError, match="workspace_path must be absolute"):
        DeploymentConfig.from_file(path)


def test_rejects_cluster_name_that_cannot_form_compose_project(tmp_path: Path):
    for filename in ("internal.secret", "postgres.secret", "minio.secret"):
        write_secret(tmp_path / filename, "secret")
    path = tmp_path / "deployment.toml"
    path.write_text(config_text().replace('name = "loom-prod"', 'name = "bad name"'), encoding="utf-8")

    with pytest.raises(DeploymentConfigError, match="cluster.name contains unsupported characters"):
        DeploymentConfig.from_file(path)


def test_ipv6_minio_console_url_is_bracketed(tmp_path: Path):
    for filename in ("internal.secret", "postgres.secret", "minio.secret"):
        write_secret(tmp_path / filename, "secret")
    path = tmp_path / "deployment.toml"
    path.write_text(config_text().replace('10.0.0.10', '2001:db8::10'), encoding="utf-8")

    config = DeploymentConfig.from_file(path)

    assert config.urls.minio_console == "http://[2001:db8::10]:9001"


def test_rejects_machine_names_that_collide_after_compose_normalization(tmp_path: Path):
    for filename in ("internal.secret", "postgres.secret", "minio.secret"):
        write_secret(tmp_path / filename, "secret")
    text = config_text().replace('name = "worker-a"', 'name = "Worker-A"') + '''

[[machines]]
name = "worker-a-copy"
ssh_host = "10.0.0.13"
ssh_user = "ubuntu"
role = "slave"
service_id = "slave-b"
'''
    text = text.replace('name = "worker-a-copy"', 'name = "worker-a"')
    path = tmp_path / "deployment.toml"
    path.write_text(text, encoding="utf-8")

    with pytest.raises(DeploymentConfigError, match="project names must be unique"):
        DeploymentConfig.from_file(path)


def test_rejects_invalid_secret_encoding_and_ssh_control_characters(tmp_path: Path):
    (tmp_path / "internal.secret").write_bytes(b"\xff")
    (tmp_path / "postgres.secret").write_text("secret", encoding="utf-8")
    (tmp_path / "minio.secret").write_text("secret", encoding="utf-8")
    path = tmp_path / "deployment.toml"
    path.write_text(config_text(), encoding="utf-8")

    with pytest.raises(DeploymentConfigError, match="internal_secret_file cannot be read"):
        DeploymentConfig.from_file(path)

    (tmp_path / "internal.secret").write_text("secret", encoding="utf-8")
    path.write_text(config_text().replace('ssh_host = "10.0.0.10"', 'ssh_host = "10.0.0.10\\nmalicious"'), encoding="utf-8")
    with pytest.raises(DeploymentConfigError, match="ssh_host must not contain"):
        DeploymentConfig.from_file(path)


def test_rejects_unknown_build_network(tmp_path: Path):
    for filename in ("internal.secret", "postgres.secret", "minio.secret"):
        write_secret(tmp_path / filename, "secret")
    path = tmp_path / "deployment.toml"
    path.write_text(config_text().replace('[cluster]', '[cluster]\nbuild_network = "bridge"'), encoding="utf-8")

    with pytest.raises(DeploymentConfigError, match="cluster.build_network"):
        DeploymentConfig.from_file(path)


def test_rejects_relative_runtime_credentials_dir(tmp_path: Path):
    for filename in ("internal.secret", "postgres.secret", "minio.secret"):
        write_secret(tmp_path / filename, "secret")
    path = tmp_path / "deployment.toml"
    path.write_text(
        config_text().replace(
            "[cluster]", '[cluster]\nruntime_credentials_dir = "credentials"'
        ),
        encoding="utf-8",
    )

    with pytest.raises(DeploymentConfigError, match="runtime_credentials_dir"):
        DeploymentConfig.from_file(path)


def test_grip_namespace_and_token_file_are_loaded(tmp_path):
    for filename in ("internal.secret", "postgres.secret", "minio.secret", "grip.secret"):
        write_secret(tmp_path / filename, "fixture-" + filename)
    path = tmp_path / "deployment.toml"
    path.write_text(config_text().replace('workspace_id = "workspace-default"', 'workspace_id = "workspace-default"\ngrip_authority = "resources.example"\npublic_base_url = "https://observer.example/"\ngrip_token_file = "grip.secret"'))
    config = DeploymentConfig.from_file(path)
    assert config.grip_authority == "resources.example"
    assert config.public_base_url == "https://observer.example"
    assert config.grip_token == "fixture-grip.secret"
    assert (tmp_path / "grip.secret") in config.secret_paths


@pytest.mark.parametrize("values", [{"grip_authority": "127.0.0.1"}, {"grip_authority": "UPPER.example"}, {"public_base_url": "https://user:secret@example.com"}])
def test_invalid_grip_deployment_configuration_is_rejected(values):
    from loom_v2.deployment.config import ClusterConfig
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        ClusterConfig(name="test", remote_dir="/test", workspace_id="default", internal_secret_file="internal", postgres_password_file="pg", minio_secret_key_file="s3", **values)
