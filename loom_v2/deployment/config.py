"""Configuration and topology validation for remote Loom deployments."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
import tomllib
from typing import Annotated, Any, Literal, Self
from urllib.parse import quote, urlsplit

from loom_orchestration.grip import Names

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError, field_validator, model_validator


class DeploymentConfigError(ValueError):
    """Raised when a deployment file cannot describe a safe topology."""


_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
NonEmptyString = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Port = Annotated[int, Field(strict=True, ge=1, le=65535)]


class ConfigModel(BaseModel):
    model_config = ConfigDict(frozen=True, str_strip_whitespace=True, hide_input_in_errors=True)


class MachineConfig(ConfigModel):
    name: NonEmptyString
    ssh_host: NonEmptyString
    ssh_port: Port = 22
    ssh_user: NonEmptyString
    role: Literal["minio", "observer", "driver", "slave"]
    service_port: Port
    console_port: Port | None = None
    service_id: Annotated[str, StringConstraints(pattern=r"^slave-[a-z0-9][a-z0-9-]{0,55}$")] | None = None
    grip_data_port: Port | None = None
    grip_data_publish_host: str = "127.0.0.1"
    consumer_ports: dict[str, dict[str, int]] = Field(default_factory=dict)
    ssh_key: Path | None = None

    @model_validator(mode="before")
    @classmethod
    def port_defaults(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        value = dict(value)
        role = value.get("role")
        if isinstance(role, str):
            role = value["role"] = role.strip().lower()
            default_port = {"minio": 9000, "observer": 8080, "driver": 8090}.get(role, 8081 if value.get("service_id") == "slave-a" else 8082)
            value.setdefault("service_port", default_port)
            if role == "minio":
                value.setdefault("console_port", 9001)
        return value

    @field_validator("name")
    @classmethod
    def valid_name(cls, value: str) -> str:
        if not _NAME_RE.fullmatch(value):
            raise ValueError("name contains unsupported characters")
        return value

    @field_validator("ssh_host", "ssh_user")
    @classmethod
    def ssh_token(cls, value: str, info) -> str:
        if any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError(f"{info.field_name} must not contain whitespace or control characters")
        return value

    @field_validator("ssh_key")
    @classmethod
    def expand_key(cls, value: Path | None) -> Path | None:
        return value.expanduser() if value is not None else None

    @model_validator(mode="after")
    def role_fields(self) -> Self:
        if self.role == "slave" and self.service_id is None:
            raise ValueError("service_id is required for a slave")
        if self.role != "slave" and self.service_id is not None:
            raise ValueError("service_id is only valid for a slave")
        if self.role != "minio" and self.console_port is not None:
            raise ValueError("console_port is only valid for minio")
        if self.role != "slave" and (self.grip_data_port is not None or self.consumer_ports):
            raise ValueError("grip_data_port and consumer_ports are only valid for a slave")
        for ports in self.consumer_ports.values():
            if set(ports) != {"host_port", "container_port"} or any(not 1 <= port <= 65535 for port in ports.values()):
                raise ValueError("consumer_ports requires host_port and container_port between 1 and 65535")
        return self

    @property
    def advertised_host(self) -> str:
        return self.ssh_host

    @property
    def endpoint_url(self) -> str:
        host = self.ssh_host
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        return f"http://{host}:{self.service_port}"


class DriverConfig(ConfigModel):
    model_config = ConfigDict(extra="forbid")
    workspace_path: NonEmptyString = "/srv/loom/workspace"

    @field_validator("workspace_path")
    @classmethod
    def absolute_workspace(cls, value: str) -> str:
        if not value.startswith("/"):
            raise ValueError("workspace_path must be absolute")
        return value


class ServiceAccessConfig(ConfigModel):
    model_config = ConfigDict(extra="forbid")
    signing_key_file: str | None = None
    signing_key_id: str = "service-access-v1"
    verification_keys: dict[str, str] = Field(default_factory=dict)
    data_bindings: dict[str, list[dict]] = Field(default_factory=dict)
    upstream_bindings: dict[str, list[dict]] = Field(default_factory=dict)
    allow_insecure: bool = False
    access_token_seconds: int = Field(default=3600, ge=60, le=3600)

    @model_validator(mode="after")
    def validate_bindings(self):
        from loom_orchestration.bindings import validate_endpoint
        from loom_orchestration.grip import Binding, GripError, uri
        for kid in [self.signing_key_id, *self.verification_keys]:
            if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", kid):
                raise ValueError("invalid service key id")
        for mapping in (self.data_bindings, self.upstream_bindings):
            for target, routes in mapping.items():
                uri(target)
                for raw in routes:
                    binding = Binding.model_validate(raw)
                    if not binding.audience or not binding.endpoint:
                        raise ValueError("operator binding needs endpoint and audience")
                    try:
                        validate_endpoint(binding, allow_insecure=self.allow_insecure)
                    except GripError as exc:
                        raise ValueError("invalid service binding: " + exc.code) from exc
        return self


class ClusterConfig(ConfigModel):
    name: NonEmptyString
    remote_dir: NonEmptyString
    workspace_id: NonEmptyString
    build_network: Literal["default", "host", "none"] = "default"
    internal_secret_file: NonEmptyString
    postgres_password_file: NonEmptyString
    minio_secret_key_file: NonEmptyString
    minio_access_key: NonEmptyString = "loom"
    runtime_credentials_dir: str | None = None
    runtime_plugin_state_dir: str = "/var/lib/loom/runtime-plugin-state"
    runtime_plugin_egress_network: NonEmptyString | None = None
    grip_authority: NonEmptyString = "loom.local"
    public_base_url: str = ""
    grip_token_file: NonEmptyString | None = None
    service_access: ServiceAccessConfig = Field(default_factory=ServiceAccessConfig)

    @field_validator("grip_authority")
    @classmethod
    def valid_grip_authority(cls, value: str) -> str:
        Names(authority=value)
        return value

    @field_validator("public_base_url")
    @classmethod
    def valid_public_base_url(cls, value: str) -> str:
        if value:
            parsed = urlsplit(value)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
                raise ValueError("public_base_url must be an HTTP(S) origin")
        return value.rstrip("/")

    @field_validator("name")
    @classmethod
    def valid_name(cls, value: str) -> str:
        if not _NAME_RE.fullmatch(value):
            raise ValueError("cluster.name contains unsupported characters")
        return value

    @field_validator("remote_dir")
    @classmethod
    def absolute_remote_dir(cls, value: str) -> str:
        if not value.startswith("/"):
            raise ValueError("cluster.remote_dir must be absolute")
        return value

    @field_validator("runtime_credentials_dir")
    @classmethod
    def absolute_runtime_credentials_dir(cls, value: str | None) -> str | None:
        if value is not None and not value.startswith("/"):
            raise ValueError("cluster.runtime_credentials_dir must be absolute")
        return value

    @field_validator("runtime_plugin_state_dir")
    @classmethod
    def absolute_runtime_plugin_state_dir(cls, value: str) -> str:
        if not value.startswith("/"):
            raise ValueError("cluster.runtime_plugin_state_dir must be absolute")
        return value


class DeploymentInput(ConfigModel):
    cluster: ClusterConfig
    machines: tuple[MachineConfig, ...] = Field(min_length=1)
    driver: DriverConfig = Field(default_factory=DriverConfig)

    @model_validator(mode="after")
    def service_entry_prerequisites(self) -> Self:
        access = self.cluster.service_access
        routes = [(target, binding) for mapping in (access.data_bindings, access.upstream_bindings)
                  for target, bindings in mapping.items() for binding in bindings]
        if not routes:
            return self
        if not access.signing_key_file or access.signing_key_id not in access.verification_keys:
            raise ValueError("service bindings require signing and matching verification key configuration")
        names = Names(self.cluster.grip_authority, self.cluster.workspace_id)
        for target, binding in routes:
            if binding.get("audience") == names.root:
                raise ValueError("service binding audience must not be the Observer relay itself")
            for machine in self.machines:
                if machine.role != "slave":
                    continue
                slave = names.name("slaves", machine.service_id)
                if (target == slave or binding.get("audience") == slave) and machine.grip_data_port is None:
                    raise ValueError(f"service bindings for {machine.service_id} require grip_data_port during node setup")
        return self


@dataclass(frozen=True)
class EndpointMap:
    observer: str
    driver: str
    minio: str
    minio_console: str
    slaves: dict[str, str]


@dataclass(frozen=True)
class DeploymentConfig:
    """Fully resolved, validated deployment configuration."""

    config_path: Path
    name: str
    remote_dir: str
    workspace_id: str
    internal_api_secret: str
    postgres_password: str
    minio_access_key: str
    minio_secret_key: str
    machines: tuple[MachineConfig, ...]
    driver: DriverConfig
    secret_paths: tuple[Path, ...]
    build_network: str = "default"
    runtime_credentials_dir: str | None = None
    runtime_plugin_state_dir: str = "/var/lib/loom/runtime-plugin-state"
    runtime_plugin_egress_network: str | None = None
    grip_authority: str = "loom.local"
    public_base_url: str = ""
    grip_token: str | None = None
    service_access: ServiceAccessConfig = field(default_factory=ServiceAccessConfig)
    service_key_material: dict[str, str] = field(default_factory=dict, repr=False)

    @classmethod
    def from_file(cls, path: str | Path) -> "DeploymentConfig":
        config_path = Path(path).expanduser().resolve()
        try:
            with config_path.open("rb") as handle:
                raw = tomllib.load(handle)
        except OSError as exc:
            raise DeploymentConfigError(f"cannot read config: {config_path}") from exc
        except tomllib.TOMLDecodeError as exc:
            raise DeploymentConfigError(f"invalid TOML: {exc}") from exc
        if not isinstance(raw, dict):
            raise DeploymentConfigError("config must be a TOML table")
        return cls._from_raw(config_path, raw)

    @classmethod
    def _from_raw(cls, config_path: Path, raw: dict[str, Any]) -> "DeploymentConfig":
        try:
            parsed = DeploymentInput.model_validate(raw)
        except ValidationError as exc:
            error = exc.errors()[0]
            location = ".".join(str(item) for item in error.get("loc", ()))
            message = str(error.get("msg", "invalid deployment configuration"))
            if location:
                message = f"{location} {message}"
            raise DeploymentConfigError(message) from exc
        cluster = parsed.cluster
        name, remote_dir, workspace_id = cluster.name, cluster.remote_dir, cluster.workspace_id
        build_network = cluster.build_network
        base_dir = config_path.parent
        internal_secret_path = _resolve_secret_path(base_dir, cluster.internal_secret_file, "cluster.internal_secret_file")
        postgres_password_path = _resolve_secret_path(base_dir, cluster.postgres_password_file, "cluster.postgres_password_file")
        minio_secret_path = _resolve_secret_path(base_dir, cluster.minio_secret_key_file, "cluster.minio_secret_key_file")
        internal_secret = _read_secret(internal_secret_path, "cluster.internal_secret_file")
        postgres_password = _read_secret(postgres_password_path, "cluster.postgres_password_file")
        minio_secret = _read_secret(minio_secret_path, "cluster.minio_secret_key_file")
        minio_access_key = cluster.minio_access_key
        machines = parsed.machines
        _validate_topology(machines)
        driver = parsed.driver
        secret_paths = [internal_secret_path, postgres_password_path, minio_secret_path]
        grip_token = None
        if cluster.grip_token_file is not None:
            grip_path = _resolve_secret_path(base_dir, cluster.grip_token_file, "cluster.grip_token_file")
            grip_token = _read_secret(grip_path, "cluster.grip_token_file")
            secret_paths.append(grip_path)
        keys = {}
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
        for key_name, key_file in {**{"grip-verify-" + k: v for k, v in cluster.service_access.verification_keys.items()},
                                   **({"grip-signing": cluster.service_access.signing_key_file} if cluster.service_access.signing_key_file else {})}.items():
            key_path = _resolve_secret_path(base_dir, key_file, "service_access key")
            try:
                value = key_path.read_text()
                key = serialization.load_pem_private_key(value.encode(), None) if key_name == "grip-signing" else serialization.load_pem_public_key(value.encode())
                if not isinstance(key, Ed25519PrivateKey if key_name == "grip-signing" else Ed25519PublicKey):
                    raise ValueError()
            except (OSError, ValueError, TypeError) as exc:
                raise DeploymentConfigError("service_access key must be an Ed25519 PEM file") from exc
            keys[key_name] = value
            secret_paths.append(key_path)
        if cluster.service_access.signing_key_file and cluster.service_access.signing_key_id not in cluster.service_access.verification_keys:
            raise DeploymentConfigError("service signing key needs a matching verification key id")
        if "grip-signing" in keys:
            signing = serialization.load_pem_private_key(keys["grip-signing"].encode(), None)
            verification = serialization.load_pem_public_key(keys["grip-verify-" + cluster.service_access.signing_key_id].encode())
            if signing.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw) != verification.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw):
                raise DeploymentConfigError("service signing and verification keys do not match")
        return cls(
            config_path=config_path,
            name=name,
            remote_dir=remote_dir,
            workspace_id=workspace_id,
            internal_api_secret=internal_secret,
            postgres_password=postgres_password,
            minio_access_key=minio_access_key.strip(),
            minio_secret_key=minio_secret,
            machines=machines,
            driver=driver,
            secret_paths=tuple(secret_paths),
            build_network=build_network,
            runtime_credentials_dir=cluster.runtime_credentials_dir,
            runtime_plugin_state_dir=cluster.runtime_plugin_state_dir,
            runtime_plugin_egress_network=cluster.runtime_plugin_egress_network,
            grip_authority=cluster.grip_authority,
            public_base_url=cluster.public_base_url,
            grip_token=grip_token,
            service_access=cluster.service_access, service_key_material=keys,
        )

    def machine(self, name_or_role: str) -> MachineConfig:
        matches = [item for item in self.machines if item.name == name_or_role or item.role == name_or_role]
        if len(matches) != 1:
            raise DeploymentConfigError(f"machine is not uniquely defined: {name_or_role}")
        return matches[0]

    @property
    def slaves(self) -> tuple[MachineConfig, ...]:
        return tuple(sorted((item for item in self.machines if item.role == "slave"), key=lambda item: item.service_id or ""))

    @property
    def urls(self) -> EndpointMap:
        observer = self.machine("observer")
        driver = self.machine("driver")
        minio = self.machine("minio")
        return EndpointMap(
            observer=observer.endpoint_url,
            driver=driver.endpoint_url,
            minio=minio.endpoint_url,
            minio_console=(
                f"http://{_url_host(minio.advertised_host)}:{minio.console_port}"
                if minio.console_port is not None
                else minio.endpoint_url
            ),
            slaves={item.service_id: item.endpoint_url for item in self.slaves if item.service_id is not None},
        )

    def database_url(self, machine: MachineConfig) -> str:
        if machine.role not in {"observer", "slave"}:
            raise DeploymentConfigError(f"database is not defined for role: {machine.role}")
        if machine.role == "observer":
            db_name = "loom_observer"
        else:
            service_id = machine.service_id
            if service_id is None:  # defensive guard for callers constructing MachineConfig directly
                raise DeploymentConfigError("slave service_id is required for database URL")
            db_name = f"loom_{service_id.replace('-', '_')}"
        return f"postgresql+asyncpg://loom:{quote(self.postgres_password, safe='')}@postgres:5432/{db_name}"


def _resolve_secret_path(base_dir: Path, raw_path: Any, field: str) -> Path:
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise DeploymentConfigError(f"{field} is required")
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    return path.resolve()


def _read_secret(path: Path, field: str) -> str:
    try:
        value = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise DeploymentConfigError(f"{field} cannot be read: {path}") from exc
    value = value.strip()
    if not value or "\n" in value or "\r" in value:
        raise DeploymentConfigError(f"{field} must contain one non-empty line")
    return value


def _url_host(host: str) -> str:
    return f"[{host}]" if ":" in host and not host.startswith("[") else host


def _compose_component(value: str) -> str:
    return value.lower().replace("_", "-").replace(".", "-")


def _validate_topology(machines: tuple[MachineConfig, ...]) -> None:
    names = [item.name for item in machines]
    if len(names) != len(set(names)):
        raise DeploymentConfigError("machine names must be unique")
    normalized_names = [_compose_component(name) for name in names]
    if len(normalized_names) != len(set(normalized_names)):
        raise DeploymentConfigError("project names must be unique")
    for role in ("minio", "driver", "observer"):
        if sum(item.role == role for item in machines) != 1:
            raise DeploymentConfigError(f"exactly one {role} machine is required")
    slaves = [item for item in machines if item.role == "slave"]
    if not slaves:
        raise DeploymentConfigError("at least one slave machine is required")
    slave_ids = [item.service_id for item in slaves]
    if len(slave_ids) != len(set(slave_ids)):
        raise DeploymentConfigError("slave service_id values must be unique")

    occupied: dict[tuple[str, int], str] = {}
    for machine in machines:
        ports = [machine.service_port]
        ports.extend(item["host_port"] for item in machine.consumer_ports.values())
        if machine.grip_data_port is not None:
            ports.append(machine.grip_data_port)
        if machine.console_port is not None:
            ports.append(machine.console_port)
        if len(ports) != len(set(ports)):
            raise DeploymentConfigError(f"{machine.name} published ports must be distinct")
        for port in ports:
            key = (machine.ssh_host, port)
            previous = occupied.get(key)
            if previous is not None:
                raise DeploymentConfigError(f"published port {port} on {machine.ssh_host} is used by {previous} and {machine.name}")
            occupied[key] = machine.name
