"""Real SDK -> Observer GRIP grants/relay -> gateway WebSocket -> real SSH."""
import json
import shutil

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat
import pytest

from capability_services.ssh_app import create_app
from loom_orchestration import LoomClient, ServicePackage, VmFileClient, GripError
from loom_v2.observer.grip import GripService
from loom_v2.observer.repository import ObserverRepository
from loom_v2.settings import Settings
from loom_v2.slave.runtime_plugins import RuntimePluginHost
from loom_v2.testing.observer import create_embedded_app, seed_embedded_slaves
from runtime_plugins.container_http_v1.runtime import DockerContainerHTTPRuntimeV1
from tests.capability_services.test_grip_services import DockerFixture, IMAGE, LocalGatewayTransport, ROOT, RuntimeAdapter, serve
from tests.capability_services.test_vm_file_streams import gateway, ssh_server  # noqa: F401
from tests.support.asgi_server import running_asgi


async def test_authenticated_public_upload_download_no_runs(gateway,tmp_path):
    name=type(gateway).__module__.split('.')[-2]
    key=Ed25519PrivateKey.generate()
    private,public=tmp_path/'sign.pem',tmp_path/'verify.pem'
    private.write_bytes(key.private_bytes(Encoding.PEM,PrivateFormat.PKCS8,NoEncryption()))
    public.write_bytes(key.public_key().public_bytes(Encoding.PEM,PublicFormat.SubjectPublicKeyInfo))
    settings=Settings(grip_token='file-test',public_base_url='http://observer',coding_agent_backend='none',
        agent_lease_ttl_seconds=300,grip_allow_insecure_services=True,grip_signing_key_file=str(private),
        runtime_plugin_instance_state_dir=str(tmp_path/'runtime-state'))
    settings.grip_verification_keys={settings.grip_signing_key_id:str(public)}
    repo=ObserverRepository(settings=settings)
    app=create_embedded_app(repo,settings=settings)
    profile=tmp_path/'file-test';profile.mkdir();shutil.copy2(tmp_path/'id_ed25519',profile/'id_ed25519')
    async with serve(create_app(gateway)) as port:
        runtime=DockerContainerHTTPRuntimeV1(slave_id='slave-a',network='internal',egress_network='egress',
            runner=DockerFixture(),http_transport=LocalGatewayTransport(port),credential_dir=tmp_path,instance_state_dir=tmp_path/'instances')
        worker=app.state.slaves['slave-a']
        worker.runtime_plugin_host=RuntimePluginHost(plugins={('service','container:http','1'):RuntimeAdapter(runtime)})
        seed_embedded_slaves(repo,app.state.slaves)
        try:
            async with running_asgi(app) as base:
                settings.public_base_url=base;app.state.service_access.settings.public_base_url=base
                app.state.grip=GripService(app,settings)
                async with LoomClient(base,token=settings.grip_token,allow_insecure_grip=True) as sdk:
                    package=await ServicePackage(ROOT/'capability_services'/name).publish(sdk,image_ref=IMAGE)
                    slave=(await sdk.resources(mcue='computing/slave'))[0]['uri']
                    activated=await sdk.invoke(slave,sdk.names.action('activate-package'),{
                        'package':package,'instance_name':'files','instance_config':json.loads(gateway.instance_path.read_text()),
                        'credential_profile':'file-test' if name=='vm_ssh_http' else None,'idempotency_key':'files'})
                    target=activated['resource']
                    for _ in range(2):
                        for report in await worker.inspect_activation_health():
                            await repo.record_capability_health(report)
                            worker.acknowledge_health_report(report.report_id)
                    before=await sdk.describe(target,refresh=True)
                    assert before['resource']['metadata']['state']=='ready'
                    files=await VmFileClient.discover(sdk,target)
                    source=tmp_path/'source';source.write_bytes(bytes(range(256))*8193)
                    remote,download=tmp_path/'remote',tmp_path/'download'
                    uploaded=await files.upload(source,str(remote))
                    downloaded=await files.download(str(remote),download)
                    assert uploaded['sha256']==downloaded['sha256']
                    assert download.read_bytes()==source.read_bytes()
                    assert not repo.runs
                    after=await sdk.describe(target,refresh=True)
                    for field in ('revision','authentication','target','package'):
                        assert after['resource']['metadata'][field]==before['resource']['metadata'][field]
                    async with LoomClient(base,token='invalid',allow_insecure_grip=True) as denied:
                        with pytest.raises(GripError):await VmFileClient.discover(denied,target)
                    await sdk.invoke(target,sdk.names.action('stop'),{'expected_revision':activated['revision'],'idempotency_key':'stop'})
                    with pytest.raises(GripError):await files.download(str(remote),tmp_path/'after-stop')
                    assert not (tmp_path/'after-stop').exists()
                    assert not list(tmp_path.glob('.loom-*'))
        finally:
            await app.state.driver.stop_execution_loop()
            await app.state.service_access.http.aclose()
            for slave in app.state.slaves.values():await slave.close()
