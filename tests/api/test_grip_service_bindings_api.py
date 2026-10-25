import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PrivateFormat, PublicFormat, NoEncryption

from loom_orchestration import LoomClient, GripError
from loom_orchestration.grip import Request
from loom_orchestration.streams import TCPStream, STREAM_PROTOCOL, StreamAborted
from loom_v2.contracts.types import CapabilityHealthReport
from loom_v2.observer.repository import ObserverRepository
from loom_v2.settings import Settings
from loom_v2.slave.grip_data import create_data_app
from loom_v2.testing.observer import create_embedded_app
from tests.support.asgi_server import running_asgi
from tests.support.package_fixture import action_export, register_test_descriptor


@pytest.fixture
async def service_setup(tmp_path):
    key = Ed25519PrivateKey.generate()
    private, public = tmp_path / 'signing.pem', tmp_path / 'verify.pem'
    private.write_bytes(key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()))
    public.write_bytes(key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo))
    settings = Settings(grip_token='owner', coding_agent_backend='none', public_base_url='http://observer',
        grip_allow_insecure_services=True, grip_signing_key_file=str(private), grip_verification_keys={'service-access-v1':str(public)},
        runtime_plugin_instance_state_dir=str(tmp_path/'private-state'))
    repo = ObserverRepository(settings=settings)
    app = create_embedded_app(repo, settings=settings)
    schema = await repo.put_content({'type':'object','properties':{'value':{'type':'integer'}},'required':['value']}, media_type='application/schema+json')
    contract = await repo.put_content({'schema_version':'io.v1','input_schema_ref':schema.model_dump(mode='json'),'output_schema_ref':schema.model_dump(mode='json')}, media_type='application/json')
    exports = [action_export(register_test_descriptor(repo, repo.names.action(name)),contract,runtime_binding={'path':'/v1/'+name,'interaction':mode})
               for name,mode in [('echo','request-reply'),('stream','byte-stream')]]
    package = await repo.register_workspace_package(settings.workspace_id, {'package_id':'test-service','package_version':'v1','package_type':'service',
        'execution':{'kind':'container:http','version':'1'},'body':{'image_ref':'registry.example/test/service@sha256:'+'d'*64,'container_port':8080,'health_path':'/healthz'},'action_exports':exports})
    activation = await repo.set_capability_desired(package.version_ref, 'slave-a', desired_state='running', idempotency_key='start')
    activation = await repo.record_capability_health(CapabilityHealthReport(report_id='ready',package_version_ref=package.version_ref, package_digest=package.package_digest,
        target_slave='slave-a',activation_state='ready',activation_revision=1))
    slave = app.state.slaves['slave-a']
    slave._cache_package(package,package.version_ref)
    slave.activations[slave._activation_key(package.version_ref,'default')] = activation
    calls = []
    async def invoke(package, export, value, **kw):
        calls.append(value)
        return {'value':value}
    async def provision(*args):
        return {'activation_state':'ready','runtime_profile':{}}
    async def close(): pass
    slave.runtime_plugin_host = SimpleNamespace(invoke=invoke,provision=provision,close=close)
    target = app.state.grip.activation_uri(package, activation)
    actions = [app.state.grip.export_uri(package,e) for e in package.action_exports]
    yield app,repo,slave,package,target,actions,calls
    await app.state.service_access.http.aclose()
    for slave in app.state.slaves.values():
        await slave.close()


async def test_service_returns_business_result_without_run_and_scopes_token(service_setup):
    app,repo,slave,package,target,actions,calls = service_setup
    async with LoomClient('http://observer',token='owner',transport=httpx.ASGITransport(app=app),allow_insecure_grip=True) as c:
        assert await c.invoke(target,actions[0],{'value':7}) == {'value':7}
        assert calls == [{'value':7}] and not repo.runs
        with pytest.raises(GripError,match='invalid_input'):
            await c.invoke(target,actions[0],{'value':'wrong'})
        token = await app.state.service_access.issue_for(SimpleNamespace(target=target,action=actions[0],audience=c.names.root,expected_activation_revision=1),subject=c.names.name('identities','owner'))
        request = Request(version='grip.v1',request_id='bad-scope',method='INVOKE',auth={'credential':token['access_token']},target=target,
            action=c.names.action('stop'),input={'expected_revision':1,'idempotency_key':'stop'},accepted_protocols=['loom-http-json.v1'])
        response = await c._client.post('/.well-known/grip',json=request.model_dump(exclude_none=True))
        assert response.json()['error_code'] == 'forbidden'
        assert len(calls) == 1 and not repo.runs


async def test_configure_is_fenced_idempotent_and_revokes_old_grants(service_setup):
    app,repo,slave,package,target,actions,calls = service_setup
    async with LoomClient('http://observer',token='owner',transport=httpx.ASGITransport(app=app),allow_insecure_grip=True) as c:
        assert await c.invoke(target,actions[0],{'value':1}) == {'value':1}
        old_credential = next(iter(c._credentials.values()))[0].credential
        payload={'expected_revision':1,'idempotency_key':'configure-one','instance_config':{'custom':'new'}}
        result=await c.invoke(target,c.names.action('configure'),payload)
        assert result == {'resource':target,'desired_revision':2,'applied_revision':2,'state':'ready'}
        assert await c.invoke(target,c.names.action('configure'),payload) == result
        with pytest.raises(GripError,match='invalid_input'):
            await c.invoke(target,c.names.action('configure'),{**payload,'idempotency_key':'stale'})
        request=Request(version='grip.v1',request_id='old',method='INVOKE',auth={'credential':old_credential},target=target,action=actions[0],input={'value':1},accepted_protocols=['loom-http-json.v1'])
        with pytest.raises(GripError,match='forbidden'):
            await app.state.service_access.invoke(request)
        assert await c.invoke(target,actions[0],{'value':2}) == {'value':2}
        current=next(iter(slave.activations.values()))
        assert current.instance_config == {'custom':'new'} and current.activation_revision == 2
        assert not repo.runs


async def test_local_recovery_backs_off_and_failed_state_survives_reconcile(service_setup):
    _, _, slave, package, _, _, _ = service_setup
    key = slave._activation_key(package.version_ref, 'default')
    recovered = []
    async def inspect(*args):
        return {'exists':False,'running':False,'healthy':False}
    async def provision(*args):
        recovered.append(True)
        raise RuntimeError('service_docker_timeout')
    slave.runtime_plugin_host.inspect = inspect
    slave.runtime_plugin_host.provision = provision
    await slave.inspect_activation_health()
    await slave.inspect_activation_health()
    assert len(recovered) == 1
    assert slave.activations[key].activation_state == 'degraded'
    # Configuration faults require an explicit action, including across startup.
    slave.activations[key] = slave.activations[key].model_copy(update={'activation_state':'failed'})
    assert await slave.reconcile_activations() == {}
    await slave.inspect_activation_health()
    assert slave.activations[key].activation_state == 'failed'
    assert len(recovered) == 1


@pytest.mark.parametrize('relay',[False,True])
async def test_real_slave_stream_and_observer_relay_close_on_stop(service_setup,relay):
    app,repo,slave,package,target,actions,calls = service_setup
    async def echo(reader,writer):
        try:
            while data:=await reader.read(65536):
                writer.write(data);await writer.drain()
        finally:
            writer.close();await writer.wait_closed()
    tcp=await asyncio.start_server(echo,'127.0.0.1',0)
    async def open_stream(package,export,payload,**kwargs):
        reader,writer=await asyncio.open_connection('127.0.0.1',tcp.sockets[0].getsockname()[1])
        stream=TCPStream(reader,writer);stream.body=payload
        return stream
    slave.runtime_plugin_host.open_stream=open_stream
    async with running_asgi(create_data_app(slave)) as data_base:
        route={'protocol':STREAM_PROTOCOL,'audience':slave.grip_data.audience,'endpoint':data_base.replace('http:','ws:')+'/grip/stream'}
        app.state.service_access.settings.grip_data_bindings={slave.grip_data.audience:[route]}
        async with running_asgi(app) as base:
            app.state.service_access.settings.public_base_url=base
            from loom_v2.observer.grip import GripService
            app.state.grip = GripService(app, app.state.service_access.settings)
            # Test the HTTP relay as a separate process boundary (no embedded shortcut).
            if relay:
                app.state.slaves={}
            async with LoomClient(base,token='owner',allow_insecure_grip=True,trusted_bindings=[route],
                    binding_preference=[base.replace('http:','ws:')+'/grip/stream' if relay else route['endpoint']]) as c:
                stream=await c.open_stream(target,actions[1],{'value':5})
                await stream.write(b'bytes-through-entry')
                assert await asyncio.wait_for(stream.read(),3)==b'bytes-through-entry'
                assert not repo.runs
                await slave.grip_data.revoke(target)
                with pytest.raises(StreamAborted):
                    await asyncio.wait_for(stream.read(),3)
                await stream.aclose()
            app.state.slaves={'slave-a':slave}
    tcp.close();await tcp.wait_closed()


@pytest.mark.parametrize('relay', [False, True])
async def test_slow_stream_consumer_backpressures_direct_and_relay_without_replay(service_setup, relay):
    app, repo, slave, _, target, actions, _ = service_setup
    gate, admitted, closed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    observed, calls = [], []
    class SlowStream:
        body = {'value':1}
        async def read(self, size):
            await closed.wait()
            return b''
        async def write(self, data):
            admitted.set()
            await gate.wait()
            assert len(data) <= 65536
            observed.append(len(data))
        async def aclose(self, code=1000):
            gate.set()
            closed.set()
    async def open_stream(*args, **kwargs):
        calls.append(True)
        return SlowStream()
    slave.runtime_plugin_host.open_stream = open_stream
    async with running_asgi(create_data_app(slave)) as data_base:
        route = {'protocol':STREAM_PROTOCOL,'audience':slave.grip_data.audience,
                 'endpoint':data_base.replace('http:','ws:')+'/grip/stream'}
        app.state.service_access.settings.grip_data_bindings = {slave.grip_data.audience:[route]}
        async with running_asgi(app) as base:
            from loom_v2.observer.grip import GripService
            app.state.service_access.settings.public_base_url = base
            app.state.grip = GripService(app, app.state.service_access.settings)
            app.state.slaves = {} if relay else {'slave-a':slave}
            try:
                async with LoomClient(base,token='owner',allow_insecure_grip=True,trusted_bindings=[route],
                        binding_preference=[base.replace('http:','ws:')+'/grip/stream' if relay else route['endpoint']]) as client:
                    async with await client.open_stream(target,actions[1],{'value':1}) as stream:
                        payload = b'x' * (32 * 1024 * 1024)
                        sending = asyncio.create_task(stream.write(payload))
                        try:
                            await asyncio.wait_for(admitted.wait(), 3)
                            await asyncio.sleep(.2)
                            assert not sending.done(), 'blocked consumer must eventually block the sender'
                            gate.set()
                            await asyncio.wait_for(sending, 10)
                            async with asyncio.timeout(10):
                                while sum(observed) < len(payload): await asyncio.sleep(.01)
                        finally:
                            gate.set()
                            sending.cancel()
                            await asyncio.gather(sending, return_exceptions=True)
                assert calls == [True] and not repo.runs
            finally:
                app.state.slaves = {'slave-a':slave}

async def test_dependency_context_contains_only_granted_actions_and_refreshes_without_owner_token(service_setup,tmp_path):
    from loom_orchestration.dependencies import DependencyContext
    from loom_v2.slave.dependencies import write, directory
    app,repo,slave,package,target,actions,calls=service_setup
    # Both source and target may use this tiny fixture activation; the declared
    # action whitelist still controls every credential and descriptor.
    async with LoomClient('http://observer',token='owner',transport=httpx.ASGITransport(app=app),allow_insecure_grip=True) as client:
        result=await client.invoke(target,client.names.action('configure'),{'expected_revision':1,'idempotency_key':'dependencies',
            'dependencies':{'echo':{'target':target,'actions':[actions[0]]}}})
        snapshot=await app.state.service_access.dependency_snapshot(target,2,'slave-a')
        dep=snapshot['dependencies']['echo']
        assert [o['action']['uri'] for o in dep['description']['offers']]==[actions[0]]
        assert all(g['action']==actions[0] for g in dep['credentials'])
        assert all(g['access_token']!='owner' for g in dep['credentials'])
        activation=next(iter(slave.activations.values()))
        write(slave.settings,slave.workspace_id,activation,snapshot)
        path=directory(slave.settings,slave.workspace_id,slave.slave_id,package.package_digest,'default')/'context.json'
        assert path.stat().st_mode & 0o777 == 0o400
        context=DependencyContext(path)
        async with context.client('echo') as (caller,declared):
            # Test transport injection only; cached descriptions and schemas
            # make this work with no Observer discovery or root credential.
            caller._client=client._client;caller._owns_client=False
            assert await caller.invoke(target,actions[0],{'value':9})=={'value':9}
            with pytest.raises(GripError,match='unsupported_action'):
                await caller.open_stream(target,actions[1],{'value':9})
        grant=dep['credentials'][0]
        req=Request(version='grip.v1',request_id='dependency-old',method='INVOKE',auth={'credential':grant['access_token']},
            target=target,action=actions[0],input={'value':1},accepted_protocols=['loom-http-json.v1'])
        await client.invoke(target,client.names.action('configure'),{'expected_revision':2,'idempotency_key':'remove-dependencies','dependencies':{}})
        with pytest.raises(GripError,match='forbidden'):await app.state.service_access.invoke(req)
        assert not path.exists()
        assert not repo.runs

@pytest.mark.parametrize('change', ['audience','workspace','action','expired','revision'])
async def test_scoped_grants_fail_before_runtime_admission(service_setup,change):
    import jwt,time
    app,repo,slave,package,target,actions,calls=service_setup
    token,_=app.state.service_access.tokens.issue(subject=slave.names.name('identities','owner'), audience=slave.grip_data.audience,
        target=target,action=actions[0],activation=target,revision=1)
    claims=jwt.decode(token,options={'verify_signature':False})
    if change=='audience':claims['aud']=slave.names.name('slaves','another')
    if change=='workspace':claims['workspace']=slave.names.root+'/other'
    if change=='action':claims['action']=actions[1]
    if change=='expired':claims['exp']=int(time.time())-1
    if change=='revision':claims['revision']=2
    forged=jwt.encode(claims,Path(app.state.service_access.settings.grip_signing_key_file).read_bytes(),algorithm='EdDSA',headers={'kid':'service-access-v1'})
    request=Request(version='grip.v1',request_id='denied',method='INVOKE',auth={'credential':forged},target=target,action=actions[0],input={'value':1},accepted_protocols=['loom-http-json.v1'])
    with pytest.raises(GripError):await slave.grip_data.invoke(request)
    assert calls==[] and slave.grip_data.calls=={} and not repo.runs
