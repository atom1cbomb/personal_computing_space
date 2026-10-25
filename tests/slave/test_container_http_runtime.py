import asyncio
from copy import deepcopy
from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from loom_v2.contracts.types import (
    CapabilityPackageVersion,
    CapabilityProvisionCommand,
    ResourceRef,
)
from runtime_plugins.container_http_v1.runtime import (
    DockerCommandResult,
    DockerContainerHTTPRuntimeV1,
)


IMAGE_REF = "registry.example/loom/http-test@sha256:" + "d" * 64


@pytest.mark.asyncio
@pytest.mark.parametrize("bound", [False, True])
async def test_stream_runtime_uses_serialized_package_identity(tmp_path, bound):
    from fastapi import FastAPI, WebSocket
    from loom_orchestration.stream_server import OpenedStream, serve_stream
    from loom_orchestration.streams import TCPStream
    from tests.support.asgi_server import running_asgi

    package = _package(["/v1/stream"])
    payload = package.model_dump(mode="json")
    assert "version_ref" not in payload
    export = payload["action_exports"][0]
    export["runtime_binding"]["interaction"] = "byte-stream"
    resource = "grip://loom.local/workspaces/workspace-default/resources/services/model"
    activation = {"target_resource_ref": {"resource_id": resource} if bound else None}
    seen = []

    async def echo(reader, writer):
        try:
            while data := await reader.read(65536):
                writer.write(data)
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    tcp = await asyncio.start_server(echo, "127.0.0.1", 0)
    app = FastAPI()

    @app.websocket("/v1/stream")
    async def stream(websocket: WebSocket):
        async def open_channel(request):
            seen.append(request)
            reader, writer = await asyncio.open_connection("127.0.0.1", tcp.sockets[0].getsockname()[1])
            return OpenedStream(TCPStream(reader, writer), {"accepted": True})
        await serve_stream(websocket, open_channel)

    try:
        async with running_asgi(app) as base:
            runtime = DockerContainerHTTPRuntimeV1(slave_id="slave-a", network="internal", instance_state_dir=tmp_path)
            async def endpoint(*_args):
                return "127.0.0.1", int(base.rsplit(":", 1)[1]), "/v1/stream", None, None
            runtime._invocation_endpoint = endpoint
            opened = await runtime.open_stream(payload, export, {"service_ref": resource},
                activation=activation, request_id="serialized-package-stream")
            async with opened:
                await opened.write(b"model-request")
                assert await opened.read() == b"model-request"
            assert seen[0].target == (resource if bound else package.version_ref)
            assert seen[0].input == {"service_ref": resource}
    finally:
        tcp.close()
        await tcp.wait_closed()


def test_instance_config_is_service_owned_json_with_a_runtime_owned_target(tmp_path):
    runtime = DockerContainerHTTPRuntimeV1(slave_id="slave-a", network="internal", instance_state_dir=tmp_path)
    config = {"api_origin": "https://volcano.example:7443", "options": {"read_only": True}}
    target = {"resource_id": "grip://loom.local/workspaces/workspace-default/resources/volcano/beijing"}
    path = runtime._write_instance_config("workspace-default", "a" * 64, "beijing", {
        "target_resource_ref": target, "instance_config": config,
    })
    assert json.loads(path.read_text()) == {"target_resource_ref": target, **config}
    assert config == {"api_origin": "https://volcano.example:7443", "options": {"read_only": True}}
    assert runtime._instance_config({"instance_config": {}}) == {}
    with pytest.raises(RuntimeError, match="activation_instance_config_invalid"):
        runtime._instance_config({"instance_config": {"target_resource_ref": "overwrite"}})
    with pytest.raises(RuntimeError, match="activation_instance_config_invalid"):
        runtime._instance_config({"instance_config": []})


@pytest.mark.asyncio
async def test_long_container_name_uses_docker_dns_id_for_health_auth_and_execution():
    package = _package(["/v1/echo"])
    runtime = DockerContainerHTTPRuntimeV1(slave_id="slave-a", network="loom-internal")
    instance = "vm-ssh-e2e-fixed3"
    name = runtime._container_name("workspace-default", package.package_digest, instance)
    assert len(name) > 63
    container_id = "123456789abc" + "0" * 52

    class Runner:
        async def run(self, args, *, timeout):
            assert args == ["inspect", name]
            return DockerCommandResult(0, json.dumps([{
                "Id": container_id,
                "Config": {"Labels": runtime._labels("workspace-default", package.version_ref, package.package_digest, instance), "Image": IMAGE_REF},
                "State": {"Running": True},
            }]))

    paths = []

    async def respond(request):
        assert request.url.host == container_id[:12]
        paths.append(request.url.path)
        return httpx.Response(200, json={"status": "authenticated"})

    runtime.runner = Runner()
    runtime.http_transport = httpx.MockTransport(respond)
    assert await runtime._health(name, 8080, "/healthz")
    assert await runtime._authentication_probe(name, 8080, "/v1/auth/status") == {"status": "authenticated"}
    result = await runtime.invoke(
        package.model_dump(mode="json"), package.action_exports[0].model_dump(mode="json"), {},
        activation={"workspace_id": "workspace-default", "target_slave": "slave-a",
                    "package_version_ref": package.version_ref, "package_digest": package.package_digest,
                    "instance_name": instance, "runtime_profile": {"container_name": name}},
        attempt_id="long-name-attempt",
    )
    assert result["value"] == {"status": "authenticated"}
    assert paths == ["/healthz", "/v1/auth/status", "/v1/echo"]


@pytest.mark.asyncio
async def test_invoke_deadline_overrides_control_timeout_and_accepts_large_output():
    package = _package(["/v1/exec"])
    runtime = DockerContainerHTTPRuntimeV1(slave_id="slave-a", network="loom-internal", request_timeout=0.01)
    name = runtime._container_name("workspace-default", package.package_digest, "default")
    output = "x" * (5 * 1024 * 1024)

    class Runner:
        async def run(self, args, *, timeout):
            return DockerCommandResult(0, json.dumps([{
                "Id": "a" * 64,
                "Config": {"Labels": runtime._labels("workspace-default", package.version_ref, package.package_digest, "default"), "Image": IMAGE_REF},
                "State": {"Running": True},
            }]))

    async def respond(request):
        assert request.extensions["timeout"]["read"] == 1
        await asyncio.sleep(0.03)
        return httpx.Response(200, json={"stdout": output})

    runtime.runner = Runner()
    runtime.http_transport = httpx.MockTransport(respond)
    result = await runtime.invoke(
        package.model_dump(mode="json"), package.action_exports[0].model_dump(mode="json"), {},
        activation={"workspace_id": "workspace-default", "target_slave": "slave-a", "instance_name": "default",
                    "package_version_ref": package.version_ref, "package_digest": package.package_digest},
        attempt_id="large-output", deadline_seconds=1,
    )
    assert result["value"]["stdout"] == output


def _ref(resource_id: str, *, descriptor_digest: str | None = None) -> dict[str, Any]:
    return ResourceRef(
        resource_id=resource_id,
        version_or_digest=descriptor_digest,
        identity_criterion="descriptor_digest" if descriptor_digest else "content_digest",
    ).model_dump(mode="json")


def _package(paths: list[str], *, health_path: str = "/healthz") -> CapabilityPackageVersion:
    return CapabilityPackageVersion(
        package_type="service",
        package_id="pkg-http-runtime",
        package_version="v1",
        package_closure_version_ref="closure-1",
        source_run_ref="run-1",
        source_closure_version_ref="version-1",
        execution={"kind": "container:http", "version": "1"},
        action_exports=[
            {
                "action_descriptor_ref": _ref(
                    f"grip://loom.local/workspaces/workspace-default/actions/http/action-{index}",
                    descriptor_digest=f"{index + 1:064x}",
                ),
                "io_contract_ref": _ref("grip://loom.local/workspaces/workspace-default/content/document/sha256/" + f"{index + 20:064x}"),
                "effect_class": "NetworkService",
                "permissions": [],
                "replay_safety": "DeclaredByPackage",
                "runtime_binding": {"path": path},
            }
            for index, path in enumerate(paths)
        ],
        body={
            "image_ref": IMAGE_REF,
            "container_port": 8080,
            "health_path": health_path,
        },
    )


@dataclass
class RecordingRunner:
    calls: list[list[str]] = field(default_factory=list)

    async def run(self, args: list[str], *, timeout: float) -> DockerCommandResult:
        self.calls.append(list(args))
        if args[:2] == ["image", "inspect"]:
            return DockerCommandResult(0, json.dumps([IMAGE_REF]))
        if args[0] == "inspect":
            return DockerCommandResult(1, stderr="No such container")
        return DockerCommandResult(0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("paths", "health_path", "error"),
    [
        (["/v1/echo", "/v1/echo"], "/healthz", "service_endpoint_duplicate"),
        (["/healthz"], "/healthz", "service_health_endpoint_conflict"),
    ],
)
async def test_endpoint_conflicts_are_rejected_before_docker_mutation(
    paths: list[str], health_path: str, error: str
) -> None:
    runner = RecordingRunner()
    runtime = DockerContainerHTTPRuntimeV1(
        slave_id="slave-test", network="loom-test-internal", runner=runner
    )

    with pytest.raises(RuntimeError, match=error):
        await runtime.provision(
            _package(paths, health_path=health_path).model_dump(mode="json"),
            CapabilityProvisionCommand(
                command_id="provision-1",
                package_version_ref="pkg-http-runtime:v1",
                package_digest="0" * 64,
                target_slave="slave-test",
                idempotency_key="provision-1",
                activation_revision=1,
            ).model_dump(mode="json"),
        )

    assert runner.calls == []


def test_credential_mount_accepts_an_operator_owned_aksk_profile(tmp_path: Path) -> None:
    profile = tmp_path / "suanlicloud-prod"
    profile.mkdir()
    (profile / "AccessKeyId").write_text("ak", encoding="utf-8")
    (profile / "SecretKey").write_text("sk", encoding="utf-8")
    runtime = DockerContainerHTTPRuntimeV1(
        slave_id="slave-test",
        network="loom-test-internal",
        credential_dir=tmp_path,
    )

    args = runtime._credential_mount_args(
        {"credential_profile": "suanlicloud-prod"}
    )

    assert args == [
        "--mount",
        f"type=bind,src={profile},dst=/run/loom/credentials,readonly",
    ]


def test_credential_mount_accepts_an_operator_owned_named_profile(tmp_path: Path) -> None:
    profile = tmp_path / "named-service-prod"
    profile.mkdir()
    (profile / "token").write_text("opaque", encoding="utf-8")
    runtime = DockerContainerHTTPRuntimeV1(
        slave_id="slave-test",
        network="loom-test-internal",
        credential_dir=tmp_path,
    )

    args = runtime._credential_mount_args(
        {"credential_profile": "named-service-prod"}
    )

    assert args == [
        "--mount",
        f"type=bind,src={profile},dst=/run/loom/credentials,readonly",
    ]


def test_credential_mount_is_opaque_to_generic_runtime(tmp_path: Path) -> None:
    profile = tmp_path / "custom-service-prod"
    profile.mkdir()
    (profile / "service-token.bin").write_bytes(b"opaque")
    runtime = DockerContainerHTTPRuntimeV1(
        slave_id="slave-test",
        network="loom-test-internal",
        credential_dir=tmp_path,
    )

    args = runtime._credential_mount_args(
        {"credential_profile": "custom-service-prod"}
    )

    assert args == [
        "--mount",
        f"type=bind,src={profile},dst=/run/loom/credentials,readonly",
    ]


@pytest.mark.asyncio
async def test_provision_uses_fixed_container_security_arguments() -> None:
    runner = RecordingRunner()

    async def respond(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/healthz"
        return httpx.Response(204)

    runtime = DockerContainerHTTPRuntimeV1(
        slave_id="slave-test",
        network="loom-test-internal",
        runner=runner,
        http_transport=httpx.MockTransport(respond),
    )
    package = _package(["/v1/echo"])
    report = await runtime.provision(
        package.model_dump(mode="json"),
        CapabilityProvisionCommand(
            command_id="provision-1",
            package_version_ref=package.version_ref,
            package_digest=package.package_digest,
            target_slave="slave-test",
            idempotency_key="provision-1",
            activation_revision=1,
        ).model_dump(mode="json"),
    )

    create = next(args for args in runner.calls if args[0] == "create")
    assert report["activation_state"] == "ready"
    assert ["--network", "loom-test-internal"] == create[
        create.index("--network") : create.index("--network") + 2
    ]
    for required in ("--read-only", "--cap-drop", "--security-opt", "--pids-limit", "--memory", "--cpus", "--tmpfs"):
        assert required in create
    for forbidden in ("--publish", "--mount", "--volume", "--device", "--privileged"):
        assert forbidden not in create
    assert all("docker.sock" not in item for item in create)
    assert create[-1] == IMAGE_REF


@pytest.mark.asyncio
async def test_authenticated_service_reports_pending_auth_and_challenge() -> None:
    runner = RecordingRunner()

    async def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/healthz":
            return httpx.Response(204)
        assert request.method == "POST"
        assert request.url.path == "/v1/auth/start"
        return httpx.Response(
            200,
            json={
                "status": "auth_required",
                "verification_uri": "https://login.example/device",
                "user_code": "ABCD-EFGH",
                "qr_payload": "https://login.example/device",
                "token": "must-not-leak",
            },
        )

    runtime = DockerContainerHTTPRuntimeV1(
        slave_id="slave-test",
        network="loom-test-internal",
        runner=runner,
        http_transport=httpx.MockTransport(respond),
    )
    package = _package(["/v1/echo"])
    package.body["authentication"] = {
        "start_path": "/v1/auth/start",
        "status_path": "/v1/auth/status",
    }
    report = await runtime.provision(
        package.model_dump(mode="json"),
        CapabilityProvisionCommand(
            command_id="provision-auth",
            package_version_ref=package.version_ref,
            package_digest=package.package_digest,
            target_slave="slave-test",
            idempotency_key="provision-auth",
            activation_revision=1,
        ).model_dump(mode="json"),
    )

    assert report["activation_state"] == "pending_auth"
    assert report["details"]["authentication"] == {
        "status": "auth_required",
        "verification_uri": "https://login.example/device",
        "user_code": "ABCD-EFGH",
        "qr_payload": "https://login.example/device",
    }


@pytest.mark.asyncio
async def test_auth_start_reuses_existing_container() -> None:
    package = _package(["/v1/echo"])
    package.body["authentication"] = {
        "start_path": "/v1/auth/start",
        "status_path": "/v1/auth/status",
    }
    workspace = "workspace-default"
    runner = RecordingRunner()

    async def run(args: list[str], *, timeout: float) -> DockerCommandResult:
        runner.calls.append(list(args))
        if args[:2] == ["image", "inspect"]:
            return DockerCommandResult(0, json.dumps([IMAGE_REF]))
        if args[0] == "inspect":
            return DockerCommandResult(
                0,
                json.dumps([{
                    "Config": {"Image": IMAGE_REF, "Labels": {
                        "io.loom.managed": "container-http-v1",
                        "io.loom.workspace": workspace,
                        "io.loom.slave": "slave-test",
                        "io.loom.package-digest": package.package_digest,
                    }},
                    "Image": IMAGE_REF,
                    "State": {"Running": True},
                }]),
            )
        return DockerCommandResult(0)

    runner.run = run  # type: ignore[method-assign]

    async def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/healthz":
            return httpx.Response(204)
        return httpx.Response(200, json={"status": "auth_required", "verification_uri": "https://login.example/device", "user_code": "ABCD-EFGH"})

    runtime = DockerContainerHTTPRuntimeV1(
        slave_id="slave-test",
        network="loom-test-internal",
        runner=runner,
        http_transport=httpx.MockTransport(respond),
    )
    result = await runtime.auth_start(
        package.model_dump(mode="json"),
        {
            "workspace_id": workspace,
            "target_slave": "slave-test",
            "package_version_ref": package.version_ref,
            "package_digest": package.package_digest,
            "runtime_profile": {},
        },
    )

    assert result["status"] == "auth_required"
    assert not any(args[0] in {"create", "start", "rm"} for args in runner.calls)


@pytest.mark.asyncio
async def test_credential_profile_is_mounted_read_only_from_operator_directory(tmp_path: Path) -> None:
    profile = tmp_path / "k8s-prod"
    profile.mkdir()
    (profile / "kubeconfig").write_text("apiVersion: v1\n", encoding="utf-8")
    runner = RecordingRunner()
    runtime = DockerContainerHTTPRuntimeV1(
        slave_id="slave-test",
        network="loom-test-internal",
        credential_dir=tmp_path,
        runner=runner,
        http_transport=httpx.MockTransport(lambda _request: httpx.Response(204)),
    )
    package = _package(["/v1/echo"])

    await runtime.provision(
        package.model_dump(mode="json"),
        CapabilityProvisionCommand(
            command_id="provision-1",
            package_version_ref=package.version_ref,
            package_digest=package.package_digest,
            target_slave="slave-test",
            idempotency_key="provision-1",
            activation_revision=1,
            credential_profile="k8s-prod",
        ).model_dump(mode="json"),
    )

    create = next(args for args in runner.calls if args[0] == "create")
    mount_index = create.index("--mount")
    assert create[mount_index + 1] == (
        f"type=bind,src={profile.resolve()},dst=/run/loom/credentials,readonly"
    )


@pytest.mark.asyncio
async def test_missing_credential_profile_fails_before_docker_mutation(tmp_path: Path) -> None:
    runner = RecordingRunner()
    runtime = DockerContainerHTTPRuntimeV1(
        slave_id="slave-test", network="loom-test-internal", credential_dir=tmp_path, runner=runner
    )
    package = _package(["/v1/echo"])

    with pytest.raises(RuntimeError, match="credential_profile_unavailable"):
        await runtime.provision(
            package.model_dump(mode="json"),
            CapabilityProvisionCommand(
                command_id="provision-1",
                package_version_ref=package.version_ref,
                package_digest=package.package_digest,
                target_slave="slave-test",
                idempotency_key="provision-1",
                activation_revision=1,
            credential_profile="missing",
            ).model_dump(mode="json"),
        )

    assert runner.calls == []


@pytest.mark.asyncio
async def test_egress_network_requires_operator_configuration_and_is_not_arbitrary(
    tmp_path: Path,
) -> None:
    runner = RecordingRunner()
    package = _package(["/v1/echo"])
    package.body["network_profile"] = "egress"
    command = CapabilityProvisionCommand(
        command_id="provision-1",
        package_version_ref=package.version_ref,
        package_digest=package.package_digest,
        target_slave="slave-test",
        idempotency_key="provision-1",
        activation_revision=1,
    ).model_dump(mode="json")
    runtime = DockerContainerHTTPRuntimeV1(
        slave_id="slave-test", network="loom-test-internal", runner=runner
    )

    with pytest.raises(RuntimeError, match="service_egress_network_unavailable"):
        await runtime.provision(package.model_dump(mode="json"), command)
    assert runner.calls == []

    runtime = DockerContainerHTTPRuntimeV1(
        slave_id="slave-test",
        network="loom-test-internal",
        egress_network="loom-egress",
        runner=runner,
        http_transport=httpx.MockTransport(lambda _request: httpx.Response(204)),
    )
    await runtime.provision(package.model_dump(mode="json"), command)
    create = next(args for args in runner.calls if args[0] == "create")
    assert create[create.index("--network") : create.index("--network") + 2] == [
        "--network",
        "loom-test-internal",
    ]
    assert create.count("--network") == 1
    connect = next(args for args in runner.calls if args[:2] == ["network", "connect"])
    assert connect[2:] == ["loom-egress", create[create.index("--name") + 1]]
    assert runner.calls.index(create) < runner.calls.index(connect) < next(
        i for i, args in enumerate(runner.calls) if args[0] == "start"
    )

    class FailedNetworkRunner(RecordingRunner):
        async def run(self, args, *, timeout):
            result = await super().run(args, timeout=timeout)
            return DockerCommandResult(1) if args[:2] == ["network", "connect"] else result

    failed = FailedNetworkRunner()
    runtime.runner = failed
    with pytest.raises(RuntimeError, match="service_container_network_failed"):
        await runtime.provision(package.model_dump(mode="json"), command)
    assert not any(args[0] == "start" for args in failed.calls)
    assert any(args[0] == "rm" for args in failed.calls)


def test_promoted_coordinate_reuses_digest_backed_container_identity() -> None:
    package = _package(["/v1/echo"])
    promoted_payload = package.model_dump(mode="json")
    promoted_payload["package_version"] = "v1-reusable"
    promoted = CapabilityPackageVersion.model_validate(promoted_payload)
    assert promoted.package_digest == package.package_digest

    runtime = DockerContainerHTTPRuntimeV1(
        slave_id="slave-test", network="loom-test-internal"
    )
    assert runtime._container_name("workspace-default", package.package_digest) == runtime._container_name(
        "workspace-default", promoted.package_digest
    )
    assert runtime._labels(
        "workspace-default", package.version_ref, package.package_digest
    ) == runtime._labels(
        "workspace-default", promoted.version_ref, promoted.package_digest
    )


@pytest.mark.asyncio
async def test_http_redirect_is_not_followed_or_replayed() -> None:
    package = _package(["/v1/echo"])
    labels = {
        "io.loom.managed": "container-http-v1",
        "io.loom.workspace": "workspace-default",
        "io.loom.slave": "slave-test",
        "io.loom.package-digest": package.package_digest,
    }
    calls = 0

    @dataclass
    class InvokeRunner:
        async def run(self, args: list[str], *, timeout: float) -> DockerCommandResult:
            if args[0] == "inspect":
                return DockerCommandResult(
                    0,
                    json.dumps(
                        [
                            {
                                "Config": {"Labels": labels, "Image": IMAGE_REF},
                                "State": {"Running": True},
                            }
                        ]
                    ),
                )
            raise AssertionError(args)

    async def redirect(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(307, headers={"location": "/v1/other"})

    runtime = DockerContainerHTTPRuntimeV1(
        slave_id="slave-test",
        network="loom-test-internal",
        runner=InvokeRunner(),
        http_transport=httpx.MockTransport(redirect),
    )

    with pytest.raises(RuntimeError, match="capability_service_http_error"):
        await runtime.invoke(
            package.model_dump(mode="json"),
            package.action_exports[0].model_dump(mode="json"),
            {"message": "hello"},
            activation={
                "workspace_id": "workspace-default",
                "target_slave": "slave-test",
                "package_version_ref": package.version_ref,
                "package_digest": package.package_digest,
                "runtime_profile": {
                    "container_name": runtime._container_name(
                        "workspace-default", package.package_digest
                    )
                },
            },
            attempt_id="attempt-1",
        )

    assert calls == 1


@pytest.mark.asyncio
async def test_reconcile_stopped_requires_exact_package_identity_before_remove() -> None:
    package = _package(["/v1/echo"])
    runner = RecordingRunner()
    runtime = DockerContainerHTTPRuntimeV1(
        slave_id="slave-test", network="loom-test-internal", runner=runner
    )

    result = await runtime.reconcile(
        [
            {
                "activation_key": "activation-1",
                "workspace_id": "workspace-default",
                "slave_id": "slave-test",
                "target_slave": "slave-test",
                "package_version_ref": "grip://loom.local/workspaces/workspace-default/packages/wrong/v1",
                "package_digest": package.package_digest,
                "package_payload": package.model_dump(mode="json"),
                "desired_state": "stopped",
                "runtime_profile": {},
            }
        ]
    )

    assert result["activations"][0]["activation_state"] == "failed"
    assert result["activations"][0]["details"]["code"] == (
        "runtime_plugin_identity_mismatch"
    )
    assert runner.calls == []


async def test_one_package_selects_credentials_per_instance_and_reconcile(tmp_path):
    runner = RecordingRunner()
    runtime = DockerContainerHTTPRuntimeV1(slave_id='slave-test', network='internal',
        credential_dir=tmp_path, runner=runner,
        http_transport=httpx.MockTransport(lambda request: httpx.Response(204)))
    package = _package(['/v1/echo'])
    digest = package.package_digest
    activations = []
    for instance in ('beijing', 'nanjing'):
        profile = tmp_path / ('k8s-' + instance)
        profile.mkdir()
        (profile / 'kubeconfig').write_text('fixture-' + instance)
        command = CapabilityProvisionCommand(command_id=instance, package_version_ref=package.version_ref,
            package_digest=digest, target_slave='slave-test', instance_name=instance,
            idempotency_key=instance, activation_revision=1, credential_profile=profile.name)
        await runtime.provision(package.model_dump(mode='json'), command.model_dump(mode='json'))
        activations.append({**command.model_dump(mode='json'), 'activation_key': instance,
            'package_payload': package.model_dump(mode='json'), 'desired_state': 'running'})
    creates = [args for args in runner.calls if args[0] == 'create']
    assert len(creates) == 2
    assert creates[0][creates[0].index('--name') + 1] != creates[1][creates[1].index('--name') + 1]
    for args, instance in zip(creates, ('beijing', 'nanjing')):
        assert f'type=bind,src={tmp_path / ("k8s-" + instance)},dst=/run/loom/credentials,readonly' in args
    runner.calls.clear()
    restored = await runtime.reconcile(activations)
    assert [a['activation_state'] for a in restored['activations']] == ['ready', 'ready']
    recreated = [args for args in runner.calls if args[0] == 'create']
    assert recreated == creates
    assert package.package_digest == digest


async def test_restart_applies_persisted_config_before_marking_container_ready(tmp_path):
    """A crash after saving revision 2 must not serve revision 1's container."""
    class StatefulRunner(RecordingRunner):
        container = None

        async def run(self, args, *, timeout):
            self.calls.append(list(args))
            if args[:2] == ["image", "inspect"]:
                return DockerCommandResult(0, json.dumps([IMAGE_REF]))
            if args[0] == "inspect":
                return DockerCommandResult(0, json.dumps([self.container])) if self.container else DockerCommandResult(1, stderr="No such container")
            if args[0] == "create":
                labels = dict(args[i + 1].split("=", 1) for i, arg in enumerate(args) if arg == "--label")
                self.container = {"Id": "a" * 64, "Config": {"Labels": labels, "Image": IMAGE_REF}, "State": {"Running": True}}
            elif args[0] == "rm":
                self.container = None
            return DockerCommandResult(0)

    package = _package(["/v1/echo"])
    runner = StatefulRunner()
    runtime = DockerContainerHTTPRuntimeV1(slave_id="slave-test", network="internal", runner=runner,
        instance_state_dir=tmp_path, http_transport=httpx.MockTransport(lambda request: httpx.Response(204)))
    command = CapabilityProvisionCommand(command_id="initial", package_version_ref=package.version_ref,
        package_digest=package.package_digest, target_slave="slave-test", instance_name="model",
        idempotency_key="initial", activation_revision=1, instance_config={"service": "old"}).model_dump(mode="json")
    assert (await runtime.provision(package.model_dump(mode="json"), command))["activation_state"] == "ready"
    old_container = deepcopy(runner.container)
    activation = {**command, "activation_key": "model", "activation_revision": 2,
        "instance_config": {"service": "new"}, "desired_state": "running", "package_payload": package.model_dump(mode="json")}
    health = await runtime.inspect(activation)
    assert health["running"] and health["identity_match"]
    assert not health["healthy"] and not health["configuration_match"]
    with pytest.raises(RuntimeError, match="service_configuration_not_applied"):
        await runtime.invoke(package.model_dump(mode="json"), package.action_exports[0].model_dump(mode="json"), {},
            activation=activation, attempt_id="stale-config")
    runner.calls.clear()
    recovered = await runtime.reconcile([activation])
    assert recovered["activations"][0]["activation_state"] == "ready"
    assert [args[0] for args in runner.calls].count("rm") == 1
    assert [args[0] for args in runner.calls].count("create") == 1
    assert runner.container != old_container
    assert (await runtime.inspect(activation))["healthy"]
    config_path = runtime._instance_config_path("workspace-default", package.package_digest, "model")
    assert json.loads(config_path.read_text())["service"] == "new"


async def test_runtime_never_uses_historical_package_credential_selection():
    runner = RecordingRunner()
    runtime = DockerContainerHTTPRuntimeV1(slave_id='slave-test', network='internal', runner=runner,
        http_transport=httpx.MockTransport(lambda request: httpx.Response(204)))
    original = _package(['/v1/echo'])
    package = original.model_dump(mode='json')
    package['body']['credential_profile'] = 'legacy-unavailable'
    command = CapabilityProvisionCommand(command_id='no-credentials', package_version_ref=original.version_ref,
        package_digest=package['package_digest'], target_slave='slave-test', idempotency_key='no-credentials', activation_revision=1)
    await runtime.provision(package, command.model_dump(mode='json'))
    create = next(args for args in runner.calls if args[0] == 'create')
    assert not any('dst=/run/loom/credentials' in arg for arg in create)


def test_credential_selection_rejects_symlink_profiles_and_entries(tmp_path):
    actual = tmp_path / 'actual'
    actual.mkdir()
    (actual / 'token').write_text('fixture')
    (tmp_path / 'linked').symlink_to(actual, target_is_directory=True)
    runtime = DockerContainerHTTPRuntimeV1(slave_id='slave-test', network='internal', credential_dir=tmp_path)
    with pytest.raises(RuntimeError, match='credential_profile_unavailable'):
        runtime._credential_mount_args({'credential_profile': 'linked'})
    profile = tmp_path / 'profile'
    profile.mkdir()
    (profile / 'token').symlink_to(actual / 'token')
    with pytest.raises(RuntimeError, match='credential_profile_unavailable'):
        runtime._credential_mount_args({'credential_profile': 'profile'})
