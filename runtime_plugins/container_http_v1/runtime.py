"""Docker/HTTP implementation owned by the ``container-http-v1`` bundle.

This module deliberately consumes plain protocol JSON.  Package contract and
digest validation belong to Slave Core; the plugin checks only the execution
contract it implements and the runtime identities needed before Docker or HTTP
operations.  Consequently the bundle neither imports Slave internals nor
maintains a second package-digest implementation.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import httpx


IMAGE_RE = re.compile(
    r"^[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?(?::[0-9]+)?/"
    r"(?:[a-z0-9]+(?:[._-][a-z0-9]+)*/)*"
    r"[a-z0-9]+(?:[._-][a-z0-9]+)*@sha256:[0-9a-f]{64}$"
)
DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
PROFILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")

RUNTIME_DESCRIPTOR: dict[str, Any] = {
    "plugin_id": "container-http-v1",
    "protocol_version": "loom.runtime-plugin/1",
    "supports": [
        {
            "package_type": "service",
            "execution": {"kind": "container:http", "version": "1"},
        }
    ],
    "runtime_descriptor_ref": "runtime://service/container:http/1",
    "runtime_descriptor_digest": (
        "58a09aa11f2e5beea73c821b780eb38b3f65c33f96e8df54359e2751dde3cf33"
    ),
}


@dataclass(frozen=True)
class DockerCommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


class DockerRunner(Protocol):
    async def run(
        self, args: list[str], *, timeout: float
    ) -> DockerCommandResult: ...


class SubprocessDockerRunner:
    """Run Docker CLI commands without a shell."""

    async def run(
        self, args: list[str], *, timeout: float
    ) -> DockerCommandResult:
        try:
            process = await asyncio.create_subprocess_exec(
                "docker",
                *args,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as exc:
            raise RuntimeError("service_runtime_unavailable") from exc
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError as exc:
            process.kill()
            await process.wait()
            raise RuntimeError("service_docker_timeout") from exc
        return DockerCommandResult(
            returncode=int(process.returncode or 0),
            stdout=stdout.decode("utf-8", errors="replace"),
            stderr=stderr.decode("utf-8", errors="replace"),
        )


class DockerContainerHTTPRuntimeV1:
    """Runtime provider for ``service + container:http/1``."""

    descriptor = RUNTIME_DESCRIPTOR

    def __init__(
        self,
        *,
        slave_id: str,
        network: str,
        memory: str = "512m",
        cpus: float = 1.0,
        pids_limit: int = 128,
        tmpfs_size: str = "64m",
        startup_timeout: float = 30.0,
        request_timeout: float = 30.0,
        response_max_bytes: int = 4 * 1024 * 1024,
        max_concurrency: int = 16,
        docker_timeout: float = 120.0,
        credential_dir: str | Path | None = None,
        instance_state_dir: str | Path | None = None,
        egress_network: str | None = None,
        runner: DockerRunner | None = None,
        http_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not slave_id or not network:
            raise ValueError("service_runtime_configuration_missing")
        numeric_values = (
            cpus,
            pids_limit,
            startup_timeout,
            request_timeout,
            response_max_bytes,
            max_concurrency,
            docker_timeout,
        )
        if any(float(value) <= 0 for value in numeric_values):
            raise ValueError("service_runtime_configuration_invalid")
        if not memory or not tmpfs_size:
            raise ValueError("service_runtime_configuration_invalid")
        self.slave_id = slave_id
        self.network = network
        self.memory = memory
        self.cpus = float(cpus)
        self.pids_limit = int(pids_limit)
        self.tmpfs_size = tmpfs_size
        self.startup_timeout = float(startup_timeout)
        self.request_timeout = float(request_timeout)
        self.response_max_bytes = int(response_max_bytes)
        self.max_concurrency = int(max_concurrency)
        self.docker_timeout = float(docker_timeout)
        self.credential_dir = (
            Path(credential_dir).expanduser().resolve()
            if credential_dir
            else None
        )
        self.instance_state_dir = Path(
            instance_state_dir or "/var/lib/loom/container-http-v1"
        ).expanduser().resolve()
        self.egress_network = egress_network or None
        self.runner = runner or SubprocessDockerRunner()
        self.http_transport = http_transport
        self._semaphores: dict[str, asyncio.Semaphore] = {}

    @staticmethod
    def _instance_config(activation: dict[str, Any] | None) -> dict[str, Any]:
        value = (activation or {}).get("instance_config") or {}
        if not isinstance(value, dict):
            raise RuntimeError("activation_instance_config_invalid")
        host = value.get("host")
        port = value.get("port", 22)
        user = value.get("user")
        if (
            not isinstance(host, str)
            or not host
            or len(host) > 253
            or any(ord(char) < 33 or char in {"/", "\\"} for char in host)
            or isinstance(port, bool)
            or not isinstance(port, int)
            or not 1 <= port <= 65535
            or not isinstance(user, str)
            or not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", user)
        ):
            raise RuntimeError("activation_instance_config_invalid")
        return {"host": host, "port": port, "user": user}

    @staticmethod
    def _persistent_state(package: dict[str, Any]) -> bool:
        body = package.get("body")
        value = body.get("persistent_state", False) if isinstance(body, dict) else False
        if not isinstance(value, bool):
            raise RuntimeError("runtime_plugin_capability_mismatch")
        return value

    def _state_volume_name(
        self, workspace_id: str, package_digest: str, instance_name: str
    ) -> str:
        safe_workspace = re.sub(r"[^a-z0-9_.-]", "-", workspace_id.lower()).strip("-")[:40] or "workspace"
        safe_slave = re.sub(r"[^a-z0-9_.-]", "-", self.slave_id.lower()).strip("-")[:24] or "slave"
        safe_instance = re.sub(r"[^a-z0-9-]", "-", instance_name.lower()).strip("-")[:63] or "default"
        return f"loom-cap-state-{safe_workspace}-{safe_slave}-{package_digest[:16]}-{safe_instance}"

    def _instance_config_path(
        self, workspace_id: str, package_digest: str, instance_name: str
    ) -> Path:
        safe_workspace = re.sub(r"[^a-z0-9_.-]", "-", workspace_id.lower()).strip("-")[:40] or "workspace"
        safe_instance = re.sub(r"[^a-z0-9-]", "-", instance_name.lower()).strip("-")[:63] or "default"
        return self.instance_state_dir / safe_workspace / package_digest / f"{safe_instance}.json"

    def _write_instance_config(
        self,
        workspace_id: str,
        package_digest: str,
        instance_name: str,
        activation: dict[str, Any],
    ) -> Path:
        path = self._instance_config_path(workspace_id, package_digest, instance_name)
        path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        payload = {
            "target_resource_ref": activation.get("target_resource_ref"),
            **self._instance_config(activation),
        }
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")), encoding="utf-8")
        # The file contains only non-secret target metadata and must be
        # readable by the fixed unprivileged service UID in the container.
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
        return path

    def _container_name(self, workspace_id: str, package_digest: str, instance_name: str = "default") -> str:
        safe_workspace = re.sub(
            r"[^a-z0-9_.-]", "-", workspace_id.lower()
        ).strip("-")[:40] or "workspace"
        safe_slave = re.sub(
            r"[^a-z0-9_.-]", "-", self.slave_id.lower()
        ).strip("-")[:24] or "slave"
        safe_instance = re.sub(r"[^a-z0-9-]", "-", instance_name.lower()).strip("-")[:63] or "default"
        suffix = "" if safe_instance == "default" else f"-{safe_instance}"
        return f"loom-cap-{safe_workspace}-{safe_slave}-{package_digest[:16]}{suffix}"

    @staticmethod
    def _instance_name(command: dict[str, Any] | None) -> str:
        value = str((command or {}).get("instance_name") or "default")
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", value):
            raise RuntimeError("activation_instance_invalid")
        return value

    @staticmethod
    def _execution_identity(package: dict[str, Any]) -> tuple[str, str]:
        execution = package.get("execution")
        if (
            package.get("package_type") != "service"
            or not isinstance(execution, dict)
            or execution.get("kind") != "container:http"
            or str(execution.get("version") or "") != "1"
        ):
            raise RuntimeError("runtime_plugin_capability_mismatch")
        package_id = package.get("package_id")
        package_version = package.get("package_version")
        digest = str(package.get("package_digest") or "").lower()
        if (
            not isinstance(package_id, str)
            or not package_id
            or not isinstance(package_version, str)
            or not package_version
            or not DIGEST_RE.fullmatch(digest)
        ):
            raise RuntimeError("runtime_plugin_identity_mismatch")
        return f"capability-package://{package_id}/{package_version}", digest

    @classmethod
    def _service_values(
        cls, package: dict[str, Any]
    ) -> tuple[str, int, str, list[dict[str, Any]]]:
        cls._execution_identity(package)
        body = package.get("body")
        exports = package.get("capability_exports")
        if not isinstance(body, dict) or not isinstance(exports, list) or not exports:
            raise RuntimeError("runtime_plugin_capability_mismatch")
        image_ref = body.get("image_ref")
        container_port = body.get("container_port")
        health_path = body.get("health_path")
        if (
            not isinstance(image_ref, str)
            or not IMAGE_RE.fullmatch(image_ref)
            or isinstance(container_port, bool)
            or not isinstance(container_port, int)
            or not 1 <= container_port <= 65535
            or not isinstance(health_path, str)
        ):
            raise RuntimeError("runtime_plugin_capability_mismatch")
        normalized_exports: list[dict[str, Any]] = []
        paths: list[str] = []
        for capability_export in exports:
            if not isinstance(capability_export, dict):
                raise RuntimeError("runtime_plugin_capability_mismatch")
            runtime_binding = capability_export.get("runtime_binding")
            path = (
                runtime_binding.get("path")
                if isinstance(runtime_binding, dict)
                else None
            )
            if not isinstance(path, str):
                raise RuntimeError("runtime_plugin_capability_mismatch")
            normalized_exports.append(capability_export)
            paths.append(path)
        if len(paths) != len(set(paths)):
            raise RuntimeError("service_endpoint_duplicate")
        if health_path in paths:
            raise RuntimeError("service_health_endpoint_conflict")
        return image_ref, container_port, health_path, normalized_exports

    @classmethod
    def _credential_profile(cls, package: dict[str, Any]) -> str | None:
        body = package.get("body")
        if not isinstance(body, dict):
            raise RuntimeError("runtime_plugin_capability_mismatch")
        profile = body.get("credential_profile")
        if profile is None:
            return None
        if not isinstance(profile, str) or not PROFILE_RE.fullmatch(profile):
            raise RuntimeError("runtime_plugin_capability_mismatch")
        return profile

    def _credential_mount_args(self, package: dict[str, Any]) -> list[str]:
        profile = self._credential_profile(package)
        if profile is None:
            return []
        if self.credential_dir is None:
            raise RuntimeError("credential_profile_unavailable")
        raw_profile_dir = self.credential_dir / profile
        if raw_profile_dir.is_symlink():
            raise RuntimeError("credential_profile_unavailable")
        profile_dir = raw_profile_dir.resolve()
        try:
            profile_dir.relative_to(self.credential_dir)
        except ValueError as exc:
            raise RuntimeError("credential_profile_unavailable") from exc
        if not profile_dir.is_dir():
            raise RuntimeError("credential_profile_unavailable")
        # The profile is an opaque, operator-selected credential bundle.  The
        # generic runtime must not know the service-specific credential format;
        # that knowledge belongs to the service image.  The profile name and
        # containment checks above are the runtime's security boundary.
        try:
            entries = list(profile_dir.iterdir())
        except OSError as exc:
            raise RuntimeError("credential_profile_unavailable") from exc
        if not entries or any(entry.is_symlink() for entry in entries):
            raise RuntimeError("credential_profile_unavailable")
        return [
            "--mount",
            f"type=bind,src={profile_dir},dst=/run/loom/credentials,readonly",
        ]

    @staticmethod
    def _network_profile(package: dict[str, Any]) -> str:
        body = package.get("body")
        profile = body.get("network_profile", "internal") if isinstance(body, dict) else None
        if profile not in {"internal", "egress"}:
            raise RuntimeError("runtime_plugin_capability_mismatch")
        return str(profile)

    def _network_args(self, package: dict[str, Any]) -> list[str]:
        if self._network_profile(package) == "internal":
            return ["--network", self.network]
        if not self.egress_network:
            raise RuntimeError("service_egress_network_unavailable")
        return ["--network", self.network, "--network", self.egress_network]

    @staticmethod
    def _authentication(package: dict[str, Any]) -> dict[str, Any] | None:
        body = package.get("body")
        authentication = body.get("authentication") if isinstance(body, dict) else None
        if authentication is None:
            return None
        if (
            not isinstance(authentication, dict)
            or not {"start_path", "status_path"}.issubset(authentication)
            or set(authentication) - {"start_path", "status_path", "browser_path", "client"}
        ):
            raise RuntimeError("runtime_plugin_capability_mismatch")
        start_path = authentication.get("start_path")
        status_path = authentication.get("status_path")
        if (
            not isinstance(start_path, str)
            or not isinstance(status_path, str)
            or not start_path.startswith("/")
            or "?" in start_path
            or "#" in start_path
            or not status_path.startswith("/")
            or "?" in status_path
            or "#" in status_path
            or start_path == status_path
        ):
            raise RuntimeError("runtime_plugin_capability_mismatch")
        value = {"start_path": start_path, "status_path": status_path}
        browser_path = authentication.get("browser_path")
        if browser_path is not None:
            if not isinstance(browser_path, str) or not browser_path.startswith("/") or "?" in browser_path or "#" in browser_path or browser_path in {start_path, status_path}:
                raise RuntimeError("runtime_plugin_capability_mismatch")
            value["browser_path"] = browser_path
        client = authentication.get("client")
        if client is not None:
            if (not isinstance(client, dict)
                    or client.get("kind") != "local-browser-bridge"
                    or client.get("protocol_version") != "1"
                    or not isinstance(client.get("entrypoint"), str)
                    or not client["entrypoint"]):
                raise RuntimeError("runtime_plugin_capability_mismatch")
            value["client"] = client
        return value

    async def auth_browser(
        self, package: dict[str, Any], activation: dict[str, Any], payload: dict[str, Any]
    ) -> dict[str, Any]:
        image_ref, port, health_path, _exports = self._service_values(package)
        workspace_id, package_ref, package_digest = self._command_context(package, {
            **activation, "target_slave": activation.get("target_slave") or activation.get("slave_id"),
        })
        instance_name = self._instance_name(activation)
        auth = self._authentication(package)
        browser_path = auth.get("browser_path") if auth else None
        if not browser_path:
            raise RuntimeError("service_browser_auth_not_configured")
        expected = self._container_name(workspace_id, package_digest, instance_name)
        profile = activation.get("runtime_profile") or {}
        name = str(profile.get("container_name") or expected)
        if name != expected:
            raise RuntimeError("service_container_identity_conflict")
        container = await self._inspect_container(name)
        if container is None or not await self._matching_container(container, labels=self._labels(workspace_id, package_ref, package_digest, instance_name), image_ref=image_ref):
            raise RuntimeError("service_container_identity_conflict")
        if not bool((container.get("State") or {}).get("Running")) or not await self._health(name, port, health_path):
            raise RuntimeError("service_container_not_ready")
        if not isinstance(payload, dict) or payload.get("action") not in {"open", "callback"}:
            raise RuntimeError("service_browser_request_invalid")
        try:
            async with httpx.AsyncClient(transport=self.http_transport, timeout=self.request_timeout,
                                         follow_redirects=False, trust_env=False) as client:
                async with client.stream("POST", f"http://{name}:{port}{browser_path}", json=payload) as response:
                    if response.status_code != 200:
                        raise RuntimeError("service_browser_proxy_failed")
                    raw = bytearray()
                    async for chunk in response.aiter_bytes():
                        raw.extend(chunk)
                        if len(raw) > 65536:
                            raise RuntimeError("service_browser_proxy_invalid")
            value = json.loads(raw)
        except (ValueError, httpx.HTTPError) as exc:
            raise RuntimeError("service_browser_proxy_invalid") from exc
        if not isinstance(value, dict):
            raise RuntimeError("service_browser_proxy_invalid")
        if payload["action"] == "callback" and value != {"status": "accepted"}:
            raise RuntimeError("service_browser_proxy_invalid")
        if payload["action"] == "open" and (
            set(value) != {"status", "location"} or value["status"] != "redirect"
            or not isinstance(value["location"], str) or len(value["location"]) > 16384
        ):
            raise RuntimeError("service_browser_proxy_invalid")
        return value

    def _command_context(
        self, package: dict[str, Any], command: dict[str, Any]
    ) -> tuple[str, str, str]:
        package_ref, package_digest = self._execution_identity(package)
        workspace_id = command.get("workspace_id")
        if (
            not isinstance(workspace_id, str)
            or not workspace_id
            or command.get("target_slave") != self.slave_id
            or command.get("package_version_ref") != package_ref
            or str(command.get("package_digest") or "").lower() != package_digest
        ):
            raise RuntimeError("runtime_plugin_identity_mismatch")
        return workspace_id, package_ref, package_digest

    def _labels(
        self, workspace_id: str, package_ref: str, package_digest: str, instance_name: str = "default"
    ) -> dict[str, str]:
        # The package digest is the immutable execution identity.  A promoted
        # reusable coordinate intentionally keeps the same digest as its
        # run-bound candidate, so the lifecycle coordinate must not fence the
        # Docker container itself.
        labels = {
            "io.loom.managed": "container-http-v1",
            "io.loom.workspace": workspace_id,
            "io.loom.slave": self.slave_id,
            "io.loom.package-digest": package_digest,
        }
        if instance_name != "default":
            labels["io.loom.instance-name"] = instance_name
        return labels

    async def _docker(self, args: list[str]) -> DockerCommandResult:
        result = await self.runner.run(args, timeout=self.docker_timeout)
        if isinstance(result, DockerCommandResult):
            return result
        if isinstance(result, tuple):
            return DockerCommandResult(
                int(result[0]),
                str(result[1] if len(result) > 1 else ""),
                str(result[2] if len(result) > 2 else ""),
            )
        return DockerCommandResult(
            int(getattr(result, "returncode", 0)),
            str(getattr(result, "stdout", "")),
            str(getattr(result, "stderr", "")),
        )

    async def _image_ready(self, image_ref: str) -> None:
        inspect = await self._docker(
            ["image", "inspect", image_ref, "--format", "{{json .RepoDigests}}"]
        )
        if inspect.returncode != 0:
            pulled = await self._docker(["pull", image_ref])
            if pulled.returncode != 0:
                raise RuntimeError("service_image_unavailable")
            inspect = await self._docker(
                [
                    "image",
                    "inspect",
                    image_ref,
                    "--format",
                    "{{json .RepoDigests}}",
                ]
            )
        if inspect.returncode != 0:
            raise RuntimeError("service_image_digest_mismatch")
        try:
            repo_digests = json.loads(inspect.stdout or "[]")
        except json.JSONDecodeError as exc:
            raise RuntimeError("service_image_digest_mismatch") from exc
        if (
            not isinstance(repo_digests, list)
            or image_ref not in {str(item) for item in repo_digests}
        ):
            raise RuntimeError("service_image_digest_mismatch")

    async def _inspect_container(self, name: str) -> dict[str, Any] | None:
        result = await self._docker(["inspect", name])
        if result.returncode != 0:
            return None
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError("service_container_inspect_failed") from exc
        if not isinstance(payload, list) or not payload or not isinstance(payload[0], dict):
            raise RuntimeError("service_container_inspect_failed")
        return payload[0]

    async def _remove_container(self, name: str) -> None:
        result = await self._docker(["rm", "--force", name])
        if result.returncode != 0 and "No such container" not in result.stderr:
            raise RuntimeError("service_container_remove_failed")

    @staticmethod
    def _labels_match(
        container: dict[str, Any], labels: dict[str, str]
    ) -> bool:
        actual = (
            container.get("Config", {}).get("Labels")
            or container.get("Labels")
            or {}
        )
        return isinstance(actual, dict) and all(
            str(actual.get(key, "")) == value for key, value in labels.items()
        )

    async def _matching_container(
        self,
        container: dict[str, Any],
        *,
        labels: dict[str, str],
        image_ref: str,
    ) -> bool:
        if not self._labels_match(container, labels):
            return False
        refs = {
            str(item)
            for item in (
                container.get("RepoDigests")
                or container.get("ImageDigest")
                or []
            )
        }
        configured = str((container.get("Config") or {}).get("Image") or "")
        actual = str(container.get("Image") or "")
        if image_ref in refs or configured == image_ref or actual == image_ref:
            return True
        if not actual:
            return False
        inspected = await self._docker(
            ["image", "inspect", image_ref, "--format", "{{.Id}}"]
        )
        return inspected.returncode == 0 and inspected.stdout.strip() == actual

    async def _health(self, container_name: str, port: int, path: str) -> bool:
        try:
            async with httpx.AsyncClient(
                transport=self.http_transport,
                timeout=self.request_timeout,
                follow_redirects=False,
            ) as client:
                async with client.stream(
                    "GET", f"http://{container_name}:{port}{path}"
                ) as response:
                    return 200 <= response.status_code < 300
        except (httpx.HTTPError, TimeoutError):
            return False

    async def _authentication_probe(
        self, container_name: str, port: int, path: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(
                transport=self.http_transport,
                timeout=self.request_timeout,
                follow_redirects=False,
            ) as client:
                response = await client.post(
                    f"http://{container_name}:{port}{path}", json=payload or {}
                )
            if not 200 <= response.status_code < 300:
                return {"status": "auth_failed"}
            value = response.json()
        except (httpx.HTTPError, TimeoutError, ValueError):
            return {"status": "auth_failed"}
        if not isinstance(value, dict):
            return {"status": "auth_failed"}
        status = str(value.get("status") or "")
        if status not in {"authenticated", "auth_required", "auth_failed"}:
            return {"status": "auth_failed"}
        # Authentication challenges are public interaction data. Keep only
        # the protocol fields and never propagate arbitrary service output.
        if status == "auth_required":
            allowed = {
                "status",
                "verification_uri",
                "user_code",
                "qr_payload",
                "expires_at",
                "poll_after_seconds",
            }
            return {key: value[key] for key in allowed if key in value}
        return {"status": status}

    async def auth_start(
        self,
        package: dict[str, Any],
        activation: dict[str, Any],
    ) -> dict[str, Any]:
        """Start a new external-service login session without recreating Docker."""
        image_ref, port, health_path, _exports = self._service_values(package)
        package_ref, package_digest = self._execution_identity(package)
        workspace_id = activation.get("workspace_id")
        target_slave = activation.get("target_slave") or activation.get("slave_id")
        if (
            not isinstance(workspace_id, str)
            or not workspace_id
            or target_slave != self.slave_id
            or activation.get("package_version_ref") != package_ref
            or str(activation.get("package_digest") or "").lower() != package_digest
        ):
            raise RuntimeError("runtime_plugin_identity_mismatch")
        authentication = self._authentication(package)
        if authentication is None:
            raise RuntimeError("service_authentication_not_configured")
        instance_name = self._instance_name(activation)
        expected_name = self._container_name(workspace_id, package_digest, instance_name)
        profile = activation.get("runtime_profile") or {}
        container_name = str(profile.get("container_name") or expected_name)
        if container_name != expected_name:
            raise RuntimeError("service_container_identity_conflict")
        container = await self._inspect_container(container_name)
        if container is None or not await self._matching_container(
            container,
            labels=self._labels(workspace_id, package_ref, package_digest, instance_name),
            image_ref=image_ref,
        ):
            raise RuntimeError("service_container_identity_conflict")
        if not bool((container.get("State") or {}).get("Running")):
            raise RuntimeError("service_container_not_running")
        if not await self._health(container_name, port, health_path):
            raise RuntimeError("service_health_check_failed")
        auth = await self._authentication_probe(
            container_name, port, authentication["start_path"],
            activation.get("browser_auth"),
        )
        return {
            **auth,
            "runtime_plugin_id": "container-http-v1",
            "runtime_profile": {
                **dict(profile),
                "container_name": container_name,
                "container_port": port,
                "authentication": auth,
            },
        }

    async def provision(
        self, package: dict[str, Any], command: dict[str, Any]
    ) -> dict[str, Any]:
        image_ref, container_port, health_path, _exports = self._service_values(
            package
        )
        workspace_id, package_ref, package_digest = self._command_context(
            package, command
        )
        instance_name = self._instance_name(command)
        credential_mount = self._credential_mount_args(package)
        network_args = self._network_args(package)
        instance_config = command.get("instance_config") or {}
        config_mount: list[str] = []
        if instance_config or command.get("target_resource_ref") is not None:
            config_path = self._write_instance_config(
                workspace_id, package_digest, instance_name, command
            )
            config_mount = [
                "--mount",
                f"type=bind,src={config_path},dst=/run/loom/instance.json,readonly",
            ]
        persistent_state = self._persistent_state(package)
        volume_name = self._state_volume_name(workspace_id, package_digest, instance_name)
        if persistent_state:
            volume = await self._docker(
                [
                    "volume",
                    "create",
                    "--label",
                    "io.loom.managed=container-http-v1",
                    "--label",
                    f"io.loom.workspace={workspace_id}",
                    "--label",
                    f"io.loom.slave={self.slave_id}",
                    "--label",
                    f"io.loom.package-digest={package_digest}",
                    "--label",
                    f"io.loom.instance-name={instance_name}",
                    volume_name,
                ]
            )
            if volume.returncode != 0:
                raise RuntimeError("service_state_volume_unavailable")
        # All cross-export path checks above happen before the first Docker
        # read or mutation.
        await self._image_ready(image_ref)
        container_name = self._container_name(workspace_id, package_digest, instance_name)
        labels = self._labels(workspace_id, package_ref, package_digest, instance_name)
        existing = await self._inspect_container(container_name)
        if existing is not None and not await self._matching_container(
            existing, labels=labels, image_ref=image_ref
        ):
            actual_labels = (
                existing.get("Config", {}).get("Labels")
                or existing.get("Labels")
                or {}
            )
            owned_identity = isinstance(actual_labels, dict) and all(
                str(actual_labels.get(key, "")) == value
                for key, value in {
                    "io.loom.managed": "container-http-v1",
                    "io.loom.workspace": workspace_id,
                    "io.loom.slave": self.slave_id,
                    "io.loom.package-digest": package_digest,
                }.items()
            )
            if not owned_identity:
                raise RuntimeError("service_container_identity_conflict")
            await self._remove_container(container_name)
            existing = None

        idempotent = False
        if existing is None:
            args = [
                "create",
                "--name",
                container_name,
                "--pull=never",
                "--restart",
                "unless-stopped",
                *network_args,
                "--read-only",
                "--user",
                "65534:65534",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--pids-limit",
                str(self.pids_limit),
                "--memory",
                self.memory,
                "--cpus",
                str(self.cpus),
                "--tmpfs",
                f"/tmp:rw,noexec,nosuid,size={self.tmpfs_size}",
            ]
            args.extend(credential_mount)
            args.extend(config_mount)
            if persistent_state:
                args.extend(
                    [
                        "--mount",
                        f"type=volume,source={volume_name},target=/var/lib/loom/service",
                    ]
                )
            for key, value in labels.items():
                args.extend(["--label", f"{key}={value}"])
            args.append(image_ref)
            created = await self._docker(args)
            if created.returncode != 0:
                raise RuntimeError("service_container_start_failed")
            started = await self._docker(["start", container_name])
            if started.returncode != 0:
                await self._remove_container(container_name)
                raise RuntimeError("service_container_start_failed")
        else:
            state = existing.get("State") or {}
            if bool(state.get("Running")):
                idempotent = True
            else:
                started = await self._docker(["start", container_name])
                if started.returncode != 0:
                    raise RuntimeError("service_container_start_failed")

        deadline = asyncio.get_running_loop().time() + self.startup_timeout
        while asyncio.get_running_loop().time() < deadline:
            if await self._health(container_name, container_port, health_path):
                authentication = self._authentication(package)
                if authentication is not None:
                    auth = await self._authentication_probe(
                        container_name,
                        container_port,
                        authentication["start_path"],
                    )
                    if auth["status"] == "auth_required":
                        return {
                            "activation_state": "pending_auth",
                            "runtime_plugin_id": "container-http-v1",
                            "runtime_profile": {
                                "container_name": container_name,
                                "container_port": container_port,
                                "authentication": auth,
                            },
                            "evidence_refs": [
                                f"container:{container_name}",
                                f"health:{package_digest[:16]}",
                            ],
                            "details": {"authentication": auth},
                            "idempotent": idempotent,
                        }
                    if auth["status"] != "authenticated":
                        return {
                            "activation_state": "failed",
                            "runtime_plugin_id": "container-http-v1",
                            "runtime_profile": {"container_name": container_name},
                            "details": {"code": "service_auth_probe_failed"},
                            "idempotent": idempotent,
                        }
                return {
                    "activation_state": "ready",
                    "runtime_plugin_id": "container-http-v1",
                    "runtime_profile": {
                        "container_name": container_name,
                        "container_port": container_port,
                    },
                    "evidence_refs": [
                        f"container:{container_name}",
                        f"health:{package_digest[:16]}",
                    ],
                    "details": {"container_name": container_name},
                    "idempotent": idempotent,
                }
            await asyncio.sleep(0.05)
        return {
            "activation_state": "failed",
            "runtime_plugin_id": "container-http-v1",
            "runtime_profile": {"container_name": container_name},
            "details": {"code": "service_health_check_failed"},
            "idempotent": idempotent,
        }

    async def invoke(
        self,
        package: dict[str, Any],
        capability_export: dict[str, Any],
        payload: dict[str, Any],
        *,
        activation: dict[str, Any],
        attempt_id: str,
        deadline_seconds: float | None = None,
    ) -> dict[str, Any]:
        image_ref, container_port, _health_path, exports = self._service_values(
            package
        )
        package_ref, package_digest = self._execution_identity(package)
        workspace_id = activation.get("workspace_id")
        target_slave = activation.get("target_slave") or activation.get("slave_id")
        if (
            not isinstance(workspace_id, str)
            or not workspace_id
            or target_slave != self.slave_id
            or activation.get("package_version_ref") != package_ref
            or str(activation.get("package_digest") or "").lower()
            != package_digest
        ):
            raise RuntimeError("runtime_plugin_identity_mismatch")
        if capability_export not in exports:
            raise RuntimeError("service_endpoint_binding_mismatch")
        runtime_binding = capability_export.get("runtime_binding") or {}
        path = str(runtime_binding.get("path") or "")
        instance_name = self._instance_name(activation)
        expected_name = self._container_name(workspace_id, package_digest, instance_name)
        profile = activation.get("runtime_profile") or {}
        container_name = str(profile.get("container_name") or expected_name)
        if container_name != expected_name:
            raise RuntimeError("service_container_identity_conflict")
        container = await self._inspect_container(container_name)
        instance_name = self._instance_name(activation)
        labels = self._labels(workspace_id, package_ref, package_digest, instance_name)
        if container is None or not await self._matching_container(
            container, labels=labels, image_ref=image_ref
        ):
            raise RuntimeError("service_container_identity_conflict")
        if not bool((container.get("State") or {}).get("Running")):
            raise RuntimeError("service_activation_not_ready")

        capacity_timeout = (
            min(self.request_timeout, float(deadline_seconds))
            if deadline_seconds is not None
            else self.request_timeout
        )
        if capacity_timeout <= 0:
            raise RuntimeError("capability_service_busy")
        semaphore_key = f"{package_digest}:{instance_name}"
        semaphore = self._semaphores.setdefault(
            semaphore_key, asyncio.Semaphore(self.max_concurrency)
        )
        try:
            await asyncio.wait_for(semaphore.acquire(), timeout=capacity_timeout)
        except asyncio.TimeoutError as exc:
            raise RuntimeError("capability_service_busy") from exc
        try:
            try:
                async with httpx.AsyncClient(
                    transport=self.http_transport,
                    timeout=capacity_timeout,
                    follow_redirects=False,
                ) as client:
                    async with client.stream(
                        "POST",
                        f"http://{container_name}:{container_port}{path}",
                        json=payload,
                        headers={
                            "Content-Type": "application/json",
                            "Accept": "application/json",
                            "X-Loom-Attempt-Id": attempt_id,
                        },
                    ) as response:
                        if not 200 <= response.status_code < 300:
                            raise RuntimeError("capability_service_http_error")
                        body = bytearray()
                        async for chunk in response.aiter_bytes():
                            body.extend(chunk)
                            if len(body) > self.response_max_bytes:
                                raise RuntimeError(
                                    "capability_service_response_too_large"
                                )
            except (httpx.TimeoutException, TimeoutError) as exc:
                raise RuntimeError("capability_service_timeout") from exc
            except httpx.HTTPError as exc:
                raise RuntimeError("capability_service_http_error") from exc
            try:
                value = json.loads(body)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise RuntimeError("capability_service_invalid_json") from exc
            return {
                "value": value,
                "replay_safety": str(capability_export.get("replay_safety") or ""),
                "runtime_plugin_id": "container-http-v1",
            }
        finally:
            semaphore.release()

    async def inspect(self, activation: dict[str, Any]) -> dict[str, Any]:
        package = activation.get("package_payload") or activation.get("package")
        if not isinstance(package, dict):
            raise RuntimeError("runtime_plugin_identity_mismatch")
        image_ref, port, health_path, _exports = self._service_values(package)
        package_ref, package_digest = self._execution_identity(package)
        workspace_id = activation.get("workspace_id")
        target_slave = activation.get("target_slave") or activation.get("slave_id")
        if (
            not isinstance(workspace_id, str)
            or not workspace_id
            or target_slave != self.slave_id
            or activation.get("package_version_ref") != package_ref
            or str(activation.get("package_digest") or "").lower()
            != package_digest
        ):
            raise RuntimeError("runtime_plugin_identity_mismatch")
        instance_name = self._instance_name(activation)
        expected_name = self._container_name(workspace_id, package_digest, instance_name)
        profile = activation.get("runtime_profile") or {}
        name = str(profile.get("container_name") or expected_name)
        if name != expected_name:
            raise RuntimeError("service_container_identity_conflict")
        container = await self._inspect_container(name)
        if container is None:
            return {
                "exists": False,
                "running": False,
                "identity_match": False,
                "healthy": False,
            }
        identity_match = await self._matching_container(
            container,
            labels=self._labels(workspace_id, package_ref, package_digest, instance_name),
            image_ref=image_ref,
        )
        running = bool((container.get("State") or {}).get("Running"))
        healthy = bool(
            identity_match
            and running
            and await self._health(name, port, health_path)
        )
        result = {
            "exists": True,
            "running": running,
            "identity_match": identity_match,
            "healthy": healthy,
        }
        authentication = self._authentication(package)
        if authentication is not None and healthy:
            auth = await self._authentication_probe(
                name, port, authentication["status_path"]
            )
            result["authentication"] = auth
            result["authenticated"] = auth["status"] == "authenticated"
        return result

    async def deprovision(self, command: dict[str, Any]) -> dict[str, Any]:
        workspace_id = command.get("workspace_id")
        package_ref = command.get("package_version_ref")
        package_digest = str(command.get("package_digest") or "").lower()
        if (
            not isinstance(workspace_id, str)
            or not workspace_id
            or not isinstance(package_ref, str)
            or not package_ref.startswith("capability-package://")
            or not DIGEST_RE.fullmatch(package_digest)
            or command.get("target_slave") != self.slave_id
        ):
            raise RuntimeError("runtime_plugin_identity_mismatch")
        instance_name = self._instance_name(command)
        name = self._container_name(workspace_id, package_digest, instance_name)
        existing = await self._inspect_container(name)
        if existing is None:
            return {
                "activation_state": "stopped",
                "runtime_plugin_id": "container-http-v1",
                "idempotent": True,
            }
        if not self._labels_match(
            existing, self._labels(workspace_id, package_ref, package_digest, instance_name)
        ):
            raise RuntimeError("service_container_identity_conflict")
        await self._remove_container(name)
        return {
            "activation_state": "stopped",
            "runtime_plugin_id": "container-http-v1",
            "idempotent": False,
        }

    async def reconcile(
        self, activations: list[dict[str, Any]]
    ) -> dict[str, Any]:
        results: list[dict[str, Any]] = []
        for activation in activations:
            activation_key = activation.get("activation_key")
            package = activation.get("package_payload") or activation.get("package")
            try:
                if not isinstance(package, dict):
                    raise RuntimeError("runtime_plugin_identity_mismatch")
                package_ref, package_digest = self._execution_identity(package)
                workspace_id = activation.get("workspace_id")
                target_slave = activation.get("target_slave") or activation.get(
                    "slave_id"
                )
                if (
                    not isinstance(workspace_id, str)
                    or not workspace_id
                    or target_slave != self.slave_id
                    or activation.get("package_version_ref") != package_ref
                    or str(activation.get("package_digest") or "").lower()
                    != package_digest
                ):
                    raise RuntimeError("runtime_plugin_identity_mismatch")
                desired_state = str(activation.get("desired_state") or "running")
                if desired_state == "stopped":
                    instance_name = self._instance_name(activation)
                    expected_name = self._container_name(workspace_id, package_digest, instance_name)
                    profile = activation.get("runtime_profile") or {}
                    name = str(profile.get("container_name") or expected_name)
                    if name != expected_name:
                        raise RuntimeError("service_container_identity_conflict")
                    existing = await self._inspect_container(name)
                    if existing is not None:
                        labels = self._labels(
                            workspace_id, package_ref, package_digest, instance_name
                        )
                        if not self._labels_match(existing, labels):
                            raise RuntimeError(
                                "service_container_identity_conflict"
                            )
                        await self._remove_container(name)
                    results.append(
                        {
                            "activation_key": activation_key,
                            "activation_state": "stopped",
                            "runtime_plugin_id": "container-http-v1",
                        }
                    )
                    continue

                inspected = await self.inspect(activation)
                if inspected.get("identity_match") and inspected.get("healthy"):
                    auth = inspected.get("authentication")
                    state = "ready"
                    profile = dict(activation.get("runtime_profile") or {})
                    if auth is not None:
                        profile["authentication"] = auth
                        state = {"authenticated": "ready", "auth_required": "pending_auth"}.get(auth["status"], "degraded")
                    results.append(
                        {
                            "activation_key": activation_key,
                            "inspect": inspected,
                            "activation_state": state,
                            "runtime_profile": profile,
                            "runtime_plugin_id": "container-http-v1",
                        }
                    )
                    continue
                if inspected.get("exists") and not inspected.get("identity_match"):
                    raise RuntimeError("service_container_identity_conflict")
                command = {
                    "command_id": f"reconcile-{activation_key or package_digest}",
                    "package_version_ref": package_ref,
                    "package_digest": package_digest,
                    "target_slave": self.slave_id,
                    "workspace_id": workspace_id,
                    "idempotency_key": f"reconcile-{package_digest}",
                    "instance_name": self._instance_name(activation),
                    "target_resource_ref": activation.get("target_resource_ref"),
                    "instance_config": dict(activation.get("instance_config") or {}),
                }
                provisioned = await self.provision(package, command)
                results.append({"activation_key": activation_key, **provisioned})
            except Exception as exc:
                results.append(
                    {
                        "activation_key": activation_key,
                        "activation_state": "failed",
                        "runtime_plugin_id": "container-http-v1",
                        "details": {
                            "code": str(exc) or "runtime_plugin_error"
                        },
                    }
                )
        return {"activations": results}

    async def close(self) -> None:
        self._semaphores.clear()


__all__ = [
    "DockerCommandResult",
    "DockerRunner",
    "SubprocessDockerRunner",
    "DockerContainerHTTPRuntimeV1",
    "RUNTIME_DESCRIPTOR",
]
