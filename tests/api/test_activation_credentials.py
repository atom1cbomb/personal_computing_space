import json
from pathlib import Path

import httpx
import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from loom_orchestration import GripError, ServicePackage
from loom_v2.contracts.types import CapabilityProvisionCommand
from loom_v2.slave.runtime_plugins import RuntimePluginHost
from loom_v2.slave.service import SlaveService
from runtime_plugins.container_http_v1.runtime import DockerContainerHTTPRuntimeV1
from tests.api.test_grip import setup, identities
from tests.capability_services.test_grip_services import DockerFixture, RuntimeAdapter, IMAGE


async def test_independent_credentials_survive_retry_stop_restart_and_are_private(setup):
    app, repo, client, _ = setup
    slave, package, _ = await identities(client)
    created = {}
    for name, profile in (("beijing", "k8s-bj"), ("nanjing", "k8s-nj")):
        payload = {"package": package, "instance_name": name, "credential_profile": profile,
                   "instance_config": {"host": name + ".example"}, "idempotency_key": name}
        result = await client.invoke(slave, client.names.action("activate-package"), payload)
        assert result == await client.invoke(slave, client.names.action("activate-package"), payload)
        assert result["state"] == "ready"
        stored = next(a for a in app.state.slaves['slave-a'].activations.values() if a.instance_name == name)
        assert stored.credential_profile == profile
        description = json.dumps(await client.describe(result["resource"]))
        assert "credential_profile" not in description and profile not in description
        with pytest.raises(GripError, match="invalid_input"):
            await client.invoke(slave, client.names.action("activate-package"), {**payload, "credential_profile": "other-account"})
        created[name] = result
    raw = await repo.get_capability_package(package)
    assert {a.package_digest for a in await repo.list_package_activations(raw)} == {raw.package_digest}
    selected = created['beijing']
    stopped = await client.invoke(selected['resource'], client.names.action('stop'), {
        'expected_revision': selected['revision'], 'idempotency_key': 'stop-beijing',
    })
    assert stopped['state'] == 'stopped'
    # Omission preserves the existing binding, including after stopping.
    restarted = await client.invoke(slave, client.names.action('activate-package'), {
        'package': package, 'instance_name': 'beijing', 'idempotency_key': 'restart-beijing',
    })
    assert restarted['resource'] == selected['resource'] and restarted['revision'] == 3
    a = next(a for a in await repo.list_package_activations(raw) if a.instance_name == 'beijing')
    assert a.credential_profile == 'k8s-bj'
    local = next(a for a in app.state.slaves['slave-a'].activations.values() if a.instance_name == 'beijing')
    assert local.credential_profile == 'k8s-bj'
    assert local.instance_config == {'host': 'beijing.example'}
    # Even a direct internal replay cannot swap credentials on an existing revision.
    bad = CapabilityProvisionCommand(command_id='bad-replay', package_version_ref=raw.version_ref,
        package_digest=raw.package_digest, target_slave='slave-a', idempotency_key=local.last_idempotency_key,
        activation_revision=local.activation_revision, instance_name='beijing', credential_profile='other-account')
    with pytest.raises(RuntimeError, match='activation_instance_binding_conflict'):
        await app.state.slaves['slave-a'].provision(bad, raw)


@pytest.mark.parametrize('profile', ['', '../outside', '/tmp/key', 'bad name', 'x' * 97, 123])
async def test_invalid_credential_selection_is_rejected_before_activation(setup, profile):
    _, repo, client, _ = setup
    slave, package, _ = await identities(client)
    with pytest.raises(GripError, match='invalid_input'):
        await client.invoke(slave, client.names.action('activate-package'), {
            'package': package, 'credential_profile': profile, 'idempotency_key': 'invalid',
        })
    assert not await repo.list_package_activations(await repo.get_capability_package(package))


async def test_slave_restart_recreates_each_instance_with_persisted_credentials(setup, tmp_path):
    _, repo, client, _ = setup
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'slave.db'}")
    docker = DockerFixture()
    credential_dir = tmp_path / "credentials"
    for name in ("team-a", "team-b"):
        profile = credential_dir / name
        profile.mkdir(parents=True)
        (profile / "outline_key").write_text("fixture-" + name)

    def service():
        runtime = DockerContainerHTTPRuntimeV1(
            slave_id="slave-a", network="internal", egress_network="egress", credential_dir=credential_dir, runner=docker,
            http_transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"status": "authenticated"})),
        )
        return SlaveService("slave-a", engine=engine, content_store=repo.content_store,
                            runtime_plugin_host=RuntimePluginHost(plugins={"http": RuntimeAdapter(runtime)}))

    original = service()
    await original.init_db()
    package_uri = await ServicePackage(Path(__file__).resolve().parents[2] / "capability_services/outline_http").publish(client, image_ref=IMAGE)
    package = await repo.get_capability_package(package_uri)
    restored = None
    try:
        for name in ("team-a", "team-b"):
            command = CapabilityProvisionCommand(
                command_id=name, package_version_ref=package_uri, package_digest=package.package_digest,
                target_slave="slave-a", instance_name=name, credential_profile=name,
                idempotency_key=name, activation_revision=1,
            )
            report = await original.provision(command, package)
            assert report.activation_state == "ready", report
        original_creates = [args for args in docker.calls if args[0] == "create"]
        assert len(original_creates) == 2
        # A different coordinate with the same digest shares the runtime identity;
        # it cannot silently reuse a container mounted with another account.
        alias = package.model_copy(update={"package_version": "alias"})
        conflicting = CapabilityProvisionCommand(
            command_id="alias", package_version_ref=alias.version_ref, package_digest=alias.package_digest,
            target_slave="slave-a", instance_name="team-a", credential_profile="team-b",
            idempotency_key="alias", activation_revision=1,
        )
        before = list(docker.calls)
        with pytest.raises(RuntimeError, match="activation_instance_binding_conflict"):
            await original.provision(conflicting, alias)
        assert docker.calls == before
        await original.close()
        # Both process memory and containers disappear. Restore only from the DB.
        docker.containers.clear()
        docker.calls.clear()
        restored = service()
        await restored.init_db()
        assert {a.instance_name: a.credential_profile for a in restored.activations.values()} == {
            "team-a": "team-a", "team-b": "team-b",
        }
        assert all(a.activation_state == "ready" and a.activation_revision == 1 for a in restored.activations.values())
        assert [args for args in docker.calls if args[0] == "create"] == original_creates
        for args, name in zip(original_creates, ("team-a", "team-b")):
            assert f"type=bind,src={credential_dir / name},dst=/run/loom/credentials,readonly" in args
    finally:
        await original.close()
        if restored is not None:
            await restored.close()
        await engine.dispose()
