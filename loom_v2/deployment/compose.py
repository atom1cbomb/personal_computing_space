"""Deterministic Docker Compose documents for one deployment machine."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any

from .config import DeploymentConfig, MachineConfig


@dataclass(frozen=True)
class RenderedProject:
    """Files and preview text produced for one machine's Compose project."""

    machine: str
    role: str
    project_name: str
    compose_text: str
    env_text: str
    secret_files: dict[str, str]
    redaction_values: tuple[str, ...] = ()

    @property
    def redacted_preview(self) -> str:
        preview = f"{self.compose_text}\n{self.env_text}"
        for secret in self.secret_files.values():
            preview = preview.replace(secret, "<redacted>")
        for value in self.redaction_values:
            preview = preview.replace(value, "<redacted>")
        return preview


def render_project(config: DeploymentConfig, machine_name: str) -> RenderedProject:
    machine = config.machine(machine_name)
    project_name = _project_name(config.name, machine.name)
    if machine.role == "observer":
        document, env = _render_observer(config, machine)
    elif machine.role == "slave":
        document, env = _render_slave(config, machine)
    elif machine.role == "driver":
        document, env = _render_driver(config, machine)
    elif machine.role == "minio":
        document, env = _render_minio(config, machine)
    else:  # pragma: no cover - config validation prevents this
        raise ValueError(f"unsupported role: {machine.role}")
    secret_files = {
        "internal_api_secret": config.internal_api_secret,
        "postgres_password": config.postgres_password,
        "minio_secret_key": config.minio_secret_key,
    }
    if machine.role == "observer" and config.grip_token is not None:
        secret_files["grip_token"] = config.grip_token
    if machine.role in {"observer", "slave"}:
        app = document["services"][machine.role]
        access = config.service_access
        keys = {k: v for k, v in config.service_key_material.items() if machine.role == "observer" or k != "grip-signing"}
        secret_files.update(keys)
        for name in keys:
            document["secrets"][name] = {"file": "./secrets/" + name}
            app["secrets"].append(name)
        app["environment"].update({
            "LOOM_GRIP_AUTHORITY": config.grip_authority,
            "LOOM_GRIP_VERIFICATION_KEYS": json.dumps({kid: "/run/secrets/grip-verify-" + kid for kid in access.verification_keys}),
            "LOOM_GRIP_ALLOW_INSECURE_SERVICES": str(access.allow_insecure).lower(),
        })
        if machine.role == "observer":
            app["environment"].update({
                "LOOM_GRIP_SIGNING_KEY_FILE": "/run/secrets/grip-signing" if access.signing_key_file else "",
                "LOOM_GRIP_SIGNING_KEY_ID": access.signing_key_id,
                "LOOM_GRIP_ACCESS_TOKEN_SECONDS": str(access.access_token_seconds),
                "LOOM_GRIP_DATA_BINDINGS": json.dumps(access.data_bindings),
                "LOOM_GRIP_UPSTREAM_BINDINGS": json.dumps(access.upstream_bindings),
            })
        else:
            if machine.grip_data_port:
                app["environment"].update({"LOOM_GRIP_DATA_LISTEN_HOST": "0.0.0.0", "LOOM_GRIP_DATA_LISTEN_PORT": "8091"})
                app["ports"].append(f"{machine.grip_data_publish_host}:{machine.grip_data_port}:8091")
            app["environment"]["LOOM_RUNTIME_PLUGIN_CONSUMER_PORTS"] = json.dumps(machine.consumer_ports)
    return RenderedProject(
        machine=machine.name,
        role=machine.role,
        project_name=project_name,
        compose_text=json.dumps(document, indent=2, sort_keys=True) + "\n",
        env_text=_env_text(env),
        secret_files=secret_files,
        redaction_values=(quote_password(config.postgres_password),),
    )


def _base_service(config: DeploymentConfig, machine: MachineConfig, *, dockerfile: str) -> dict[str, Any]:
    return {
        "build": {"context": "./source", "dockerfile": dockerfile, "network": config.build_network},
        "image": f"{_project_name(config.name, machine.name)}-{machine.role}",
        "restart": "unless-stopped",
    }


def _postgres_service(volume_name: str, database: str) -> dict[str, Any]:
    return {
        "image": "postgres:16-alpine",
        "restart": "unless-stopped",
        "environment": {
            "POSTGRES_DB": database,
            "POSTGRES_PASSWORD": "${POSTGRES_PASSWORD}",
            "POSTGRES_USER": "loom",
        },
        "healthcheck": {
            "test": ["CMD-SHELL", f"pg_isready -U loom -d {database}"],
            "interval": "2s",
            "timeout": "3s",
            "retries": 30,
        },
        "volumes": [f"{volume_name}:/var/lib/postgresql/data"],
    }


def _role_environment(config: DeploymentConfig, machine: MachineConfig, database_url: str) -> dict[str, str]:
    return {
        "LOOM_DATABASE_URL": database_url.replace(quote_password(config.postgres_password), "${POSTGRES_PASSWORD_URLENCODED}"),
        "LOOM_PACKAGE_CONTRACT_DIR": "/opt/loom/package-contracts",
        "LOOM_S3_ACCESS_KEY": "${MINIO_ACCESS_KEY}",
        "LOOM_S3_BUCKET": "${LOOM_S3_BUCKET}",
        "LOOM_S3_ENDPOINT_URL": "${LOOM_S3_ENDPOINT_URL}",
        "LOOM_S3_REGION": "us-east-1",
        "LOOM_S3_SECRET_KEY": "${MINIO_SECRET_KEY}",
        "LOOM_SERVICE_NAME": machine.service_id or machine.role,
        "LOOM_WORKSPACE_ID": config.workspace_id,
    }


def _render_observer(config: DeploymentConfig, machine: MachineConfig) -> tuple[dict[str, Any], dict[str, str]]:
    database = "loom_observer"
    app = _base_service(config, machine, dockerfile="Dockerfile")
    app.update(
        {
            "command": [
                "/bin/sh",
                "-c",
                'export LOOM_INTERNAL_API_SECRET="$$(cat /run/secrets/internal_api_secret)" && exec uvicorn loom_v2.observer.app:app --host 0.0.0.0 --port 8080',
            ],
            "environment": {
                **_role_environment(config, machine, config.database_url(machine)),
                "LOOM_GRIP_AUTHORITY": config.grip_authority,
                "LOOM_GRIP_TOKEN": "${LOOM_GRIP_TOKEN:-}",
                "LOOM_PUBLIC_BASE_URL": config.public_base_url,
            },
            "ports": [f"{machine.service_port}:8080"],
            "secrets": ["internal_api_secret"],
            "depends_on": {"postgres": {"condition": "service_healthy"}},
            "volumes": [
                "./source/package_contracts:/opt/loom/package-contracts:ro"
            ],
        }
    )
    document = {
        "services": {"observer": app, "postgres": _postgres_service(f"{machine.name}-postgres-data", database)},
        "secrets": {"internal_api_secret": {"file": "./secrets/internal_api_secret"}},
        "volumes": {f"{machine.name}-postgres-data": {}},
    }
    if config.grip_token is not None:
        app["command"][2] = 'export LOOM_GRIP_TOKEN="$$(cat /run/secrets/grip_token)" && ' + app["command"][2]
        app["environment"].pop("LOOM_GRIP_TOKEN")
        app["secrets"].append("grip_token")
        document["secrets"]["grip_token"] = {"file": "./secrets/grip_token"}
    env = _common_env(config, database_url=config.database_url(machine))
    return document, env


def _render_slave(config: DeploymentConfig, machine: MachineConfig) -> tuple[dict[str, Any], dict[str, str]]:
    assert machine.service_id is not None
    database = f"loom_{machine.service_id.replace('-', '_')}"
    app = _base_service(config, machine, dockerfile="Dockerfile")
    app.update(
        {
            "command": [
                "/bin/sh",
                "-c",
                'export LOOM_INTERNAL_API_SECRET="$$(cat /run/secrets/internal_api_secret)" && exec uvicorn loom_v2.slave.app:create_app --factory --host 0.0.0.0 --port 8080',
            ],
            "environment": {
                **_role_environment(config, machine, config.database_url(machine)),
                "LOOM_OBSERVER_URL": config.urls.observer,
                "LOOM_SLAVE_ENDPOINT_URL": machine.endpoint_url,
                "LOOM_RUNTIME_PLUGIN_DIR": "/opt/loom/runtime-plugins",
                "LOOM_RUNTIME_PLUGIN_SOCKET_DIR": "/run/loom/runtime-plugins",
                "LOOM_RUNTIME_PLUGIN_NETWORK": "loom-internal",
                "LOOM_RUNTIME_PLUGIN_MEMORY": "512m",
                "LOOM_RUNTIME_PLUGIN_CPUS": "1.0",
                "LOOM_RUNTIME_PLUGIN_PIDS_LIMIT": "128",
                "LOOM_RUNTIME_PLUGIN_TMPFS_SIZE": "64m",
                "LOOM_RUNTIME_PLUGIN_INSTANCE_STATE_DIR": config.runtime_plugin_state_dir,
            },
            "ports": [f"{machine.service_port}:8080"],
            "secrets": ["internal_api_secret"],
            "depends_on": {"postgres": {"condition": "service_healthy"}},
            "volumes": [
                "./source/runtime_plugins:/opt/loom/runtime-plugins:ro",
                "./source/package_contracts:/opt/loom/package-contracts:ro",
                f"{machine.name}-runtime-plugin-sockets:/run/loom/runtime-plugins",
                f"{config.runtime_plugin_state_dir}:{config.runtime_plugin_state_dir}",
                "/var/run/docker.sock:/var/run/docker.sock",
            ],
            "networks": ["default", "loom-internal"],
        }
    )
    if machine.resource_metadata is not None:
        app["environment"]["LOOM_SLAVE_RESOURCE_METADATA"] = machine.resource_metadata.model_dump_json()
    if config.runtime_credentials_dir:
        # The runtime plugin invokes the host Docker daemon through the
        # mounted socket.  Bind sources in ``docker create`` are therefore
        # resolved by the daemon on the host, not by the Slave container.
        # Keep the operator directory at the same absolute path inside the
        # Slave so the plugin can validate it and the daemon can mount it
        # into the service container.
        app["environment"]["LOOM_RUNTIME_PLUGIN_CREDENTIAL_DIR"] = config.runtime_credentials_dir
        app["volumes"].append(f"{config.runtime_credentials_dir}:{config.runtime_credentials_dir}:ro")
    if config.runtime_plugin_egress_network:
        app["environment"]["LOOM_RUNTIME_PLUGIN_EGRESS_NETWORK"] = config.runtime_plugin_egress_network
    document = {
        "services": {"slave": app, "postgres": _postgres_service(f"{machine.name}-postgres-data", database)},
        "secrets": {"internal_api_secret": {"file": "./secrets/internal_api_secret"}},
        "volumes": {f"{machine.name}-postgres-data": {}, f"{machine.name}-runtime-plugin-sockets": {}},
        "networks": {"loom-internal": {"name": "loom-internal", "internal": True}},
    }
    env = _common_env(config, database_url=config.database_url(machine))
    env["LOOM_OBSERVER_URL"] = config.urls.observer
    return document, env


def _render_driver(config: DeploymentConfig, machine: MachineConfig) -> tuple[dict[str, Any], dict[str, str]]:
    app = _base_service(config, machine, dockerfile="Dockerfile.driver")
    environment = {
        "LOOM_AGENT_ID": "driver-default",
        "LOOM_DRIVER_URL": machine.endpoint_url,
        "LOOM_OBSERVER_URL": config.urls.observer,
        "LOOM_PACKAGE_CONTRACT_DIR": "/opt/loom/package-contracts",
        "LOOM_S3_ACCESS_KEY": "${MINIO_ACCESS_KEY}",
        "LOOM_S3_BUCKET": "${LOOM_S3_BUCKET}",
        "LOOM_S3_ENDPOINT_URL": "${LOOM_S3_ENDPOINT_URL}",
        "LOOM_S3_REGION": "us-east-1",
        "LOOM_S3_SECRET_KEY": "${MINIO_SECRET_KEY}",
        "LOOM_SERVICE_NAME": "driver",
        "LOOM_WORKSPACE_ID": config.workspace_id,
        "LOOM_WORKSPACE_ROOT": "/workspace",
    }
    app.update(
        {
            "environment": environment,
            "ports": [f"{machine.service_port}:8090"],
            "secrets": [
                "internal_api_secret",
            ],
            "volumes": [
                f"{config.driver.workspace_path}:/workspace",
                "./source/package_contracts:/opt/loom/package-contracts:ro",
                "/var/run/docker.sock:/var/run/docker.sock",
            ],
        }
    )
    document = {
        "services": {"driver": app},
        "secrets": {
            "internal_api_secret": {"file": "./secrets/internal_api_secret"},
        },
        "volumes": {},
    }
    env = {
        "MINIO_ACCESS_KEY": config.minio_access_key,
        "MINIO_SECRET_KEY": config.minio_secret_key,
        "LOOM_S3_BUCKET": "loom-content",
        "LOOM_S3_ENDPOINT_URL": config.urls.minio,
    }
    return document, env


def _render_minio(config: DeploymentConfig, machine: MachineConfig) -> tuple[dict[str, Any], dict[str, str]]:
    assert machine.console_port is not None
    minio = {
        # MinIO's current public images are published from quay.io; the old
        # Docker Hub namespace now returns repository-not-found on fresh hosts.
        "image": "quay.io/minio/minio:latest",
        "restart": "unless-stopped",
        "command": ["server", "/data", "--console-address", ":9001"],
        "environment": {
            "MINIO_ROOT_PASSWORD": "${MINIO_SECRET_KEY}",
            "MINIO_ROOT_USER": "${MINIO_ACCESS_KEY}",
        },
        "ports": [f"{machine.service_port}:9000", f"{machine.console_port}:9001"],
        "volumes": [f"{machine.name}-minio-data:/data"],
        "healthcheck": {
            "test": ["CMD-SHELL", "curl -f http://localhost:9000/minio/health/live || exit 1"],
            "interval": "2s",
            "timeout": "3s",
            "retries": 30,
        },
    }
    init = {
        "image": "quay.io/minio/mc:latest",
        "depends_on": {"minio": {"condition": "service_healthy"}},
        "environment": {
            "MINIO_ACCESS_KEY": "${MINIO_ACCESS_KEY}",
            "MINIO_SECRET_KEY": "${MINIO_SECRET_KEY}",
            "LOOM_S3_BUCKET": "${LOOM_S3_BUCKET}",
        },
        "entrypoint": ["/bin/sh", "-c"],
        "command": ['mc alias set loom http://minio:9000 "$${MINIO_ACCESS_KEY}" "$${MINIO_SECRET_KEY}" && mc mb --ignore-existing "loom/$${LOOM_S3_BUCKET}"'],
        "restart": "no",
    }
    document = {
        "services": {"minio": minio, "minio-init": init},
        "volumes": {f"{machine.name}-minio-data": {}},
    }
    env = {
        "MINIO_ACCESS_KEY": config.minio_access_key,
        "MINIO_SECRET_KEY": config.minio_secret_key,
        "LOOM_S3_BUCKET": "loom-content",
    }
    return document, env


def _common_env(config: DeploymentConfig, *, database_url: str) -> dict[str, str]:
    return {
        "LOOM_DATABASE_URL": database_url,
        "POSTGRES_PASSWORD": config.postgres_password,
        "POSTGRES_PASSWORD_URLENCODED": quote_password(config.postgres_password),
        "MINIO_ACCESS_KEY": config.minio_access_key,
        "MINIO_SECRET_KEY": config.minio_secret_key,
        "LOOM_S3_BUCKET": "loom-content",
        "LOOM_S3_ENDPOINT_URL": config.urls.minio,
    }


def _env_text(values: dict[str, str]) -> str:
    return "".join(f"{key}={_dotenv_quote(value)}\n" for key, value in sorted(values.items()))


def _dotenv_quote(value: str) -> str:
    return "'" + value.replace("'", "\\'") + "'"


def quote_password(password: str) -> str:
    """Return the URL-encoded representation used in database URLs."""

    from urllib.parse import quote

    return quote(password, safe="")


def _project_name(cluster: str, machine: str) -> str:
    return f"{cluster}-{machine}".lower().replace("_", "-").replace(".", "-")
