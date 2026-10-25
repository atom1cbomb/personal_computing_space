import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from capability_services.embedding_http.server import EmbeddingService, create_apps
from capability_services.kubernetes_http.service_access import ServiceAccess, AccessError, OWNER, classify
from capability_services.kubernetes_http.server import create_app
from loom_orchestration.grip import Request, Names
from loom_orchestration.streams import connect_stream, STREAM_PROTOCOL, StreamAborted
from tests.support.asgi_server import running_asgi

N=Names()
SERVICE=N.name('resources','services','embedding')
TARGET={'namespace':'dev-test','name':'embedding-access','port':8001,'selector':{'app':'embedding'},
        'workload':{'resource':'deployment','name':'embedding','uid':'workload-1'}}


def owned_service(uid='service-1'):
    return {'metadata':{'name':TARGET['name'],'namespace':TARGET['namespace'],'uid':uid,'annotations':{OWNER:SERVICE},
                        'ownerReferences':[ServiceAccess.owner_reference(TARGET)]},
            'spec':{'type':'ClusterIP','clusterIP':'10.1.0.2','selector':TARGET['selector'],
                    'ports':[{'name':'http','port':8001,'targetPort':8001,'protocol':'TCP'}]}}


class Gateway:
    def __init__(self,home):
        self.home=home;self.policy={'namespace':'dev-test'};self.kubeconfig=home/'kubeconfig'
        self.kubeconfig.write_text('test');self.kubectl='unused';self.commands=[];self.service=None
    def _name(self,name): return name
    def _require_auth(self): return None
    def _base_args(self): return [self.kubectl]
    def _run(self,args,stdin=None):
        self.commands.append(args)
        if 'deployment' in args:
            result={'metadata':{'uid':'workload-1'},'spec':{'template':{'metadata':{'labels':{'app':'embedding'}}}}}
        elif 'get' in args and self.service is None:
            return 1,'','Error from server (NotFound)',False
        elif 'create' in args:
            self.service=owned_service();result=self.service
        else: result=self.service
        return 0,json.dumps(result),'',False
    def close(self): pass


async def test_ensure_never_adopts_headless_unowned_or_recreated_service(tmp_path):
    gateway=Gateway(tmp_path)
    access=ServiceAccess(gateway,{'service_targets':{SERVICE:TARGET}})
    # No real subprocess in this ownership unit test.
    access.start=lambda ref: None
    assert (await access.ensure(SERVICE))['uid']=='service-1'
    assert sum('create' in c for c in gateway.commands)==1
    await access.ensure(SERVICE)
    assert sum('create' in c for c in gateway.commands)==1
    for change in ('uid','owner','headless','external','workload'):
        gateway.service=owned_service()
        if change=='uid':gateway.service['metadata']['uid']='replacement'
        if change=='owner':gateway.service['metadata']['annotations']={}
        if change=='headless':gateway.service['spec']['clusterIP']='None'
        if change=='external':gateway.service['spec']['externalIPs']=['1.2.3.4']
        if change=='workload':gateway.service['metadata']['ownerReferences'][0]['uid']='another-workload'
        with pytest.raises(AccessError):await access.ensure(SERVICE)
    with pytest.raises(AccessError,match='service_not_allowed'):
        await access.ensure(N.name('resources','services','other'))
    restored=ServiceAccess(gateway,{'service_targets':{SERVICE:TARGET}})
    assert restored.identities=={SERVICE:'service-1'}
    assert classify('Error Forbidden')=='rbac_denied'
    assert classify('Unauthorized')=='pending_auth'
    assert classify('connection refused')=='reconnecting'
    assert classify('oidc discovery error: Get https://login.example/.well-known/openid-configuration: i/o timeout')=='reconnecting'


async def test_embedding_adapter_over_real_k8s_websocket_and_httpcore(tmp_path):
    upstream=FastAPI();requests=[]
    @upstream.get('/info')
    async def info():return {'model':'actual-model','dimension':2,'device':'cuda:0'}
    @upstream.get('/health')
    async def health():return {'status':'ok','model_loaded':True}
    @upstream.post('/embed')
    async def embed(payload:dict):
        requests.append(payload)
        return {'embeddings':[[float(i),1.0] for i in range(len(payload['texts']))],'dimension':2}
    async with running_asgi(upstream) as upstream_base:
        gateway=Gateway(tmp_path);gateway.service=owned_service()
        access=ServiceAccess(gateway,{'service_targets':{SERVICE:TARGET}})
        access.identities[SERVICE]='service-1'
        access.channels[SERVICE]={'state':'ready','port':int(upstream_base.rsplit(':',1)[1]),'streams':set(),'task':None}
        async with running_asgi(create_app(gateway,access)) as k8s_base:
            opens=[]
            async def opener():
                opens.append(True)
                request=Request(version='grip.v1',request_id='embedding-test',method='INVOKE',auth={'credential':'runtime-local'},target=N.name('activations','k8s'),
                    action=N.action('open-service-stream'),input={'service_ref':SERVICE},accepted_protocols=[STREAM_PROTOCOL])
                return await connect_stream(k8s_base.replace('http:','ws:')+'/v1/services/stream',request)
            model=EmbeddingService({'service_ref':SERVICE},opener=opener)
            private,consumer=create_apps(model)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=consumer),base_url='http://adapter') as client:
                assert (await client.get('/health')).json()=={'status':'ok','model_loaded':True}
                assert (await client.get('/info')).json()=={'model':'actual-model','dimension':2,'device':'cuda:0'}
                payload={'texts':['x'*90000,'second'],'normalize':False,'instruction':'Represent this paper'}
                response=await client.post('/embed',json=payload)
                assert response.status_code==200,response.text
                assert response.json()=={'embeddings':[[0.0,1.0],[1.0,1.0]],'dimension':2}
                assert requests[-1]==payload
                response=await client.post('/embed-one',json={'text':'one','instruction':'Query'})
                assert response.json()=={'embedding':[0.0,1.0],'dimension':2}
                assert requests[-1]=={'texts':['one'],'normalize':True,'instruction':'Query'}
                assert (await client.post('/v1/model/embed',json={'texts':['hidden']})).status_code==404
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=private),base_url='http://private') as client:
                assert (await client.post('/v1/model/info',json={})).json()['model']=='actual-model'
                assert (await client.get('/embed')).status_code==404
            assert len(opens)==5
            gateway.service=owned_service('replaced')
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=consumer),base_url='http://adapter') as client:
                assert (await client.post('/embed',json={'texts':['fail']})).status_code==503
            assert len(opens)==6 and len(requests)==2  # failed admission did not replay
            await model.close()
            await access.close()

@pytest.mark.parametrize('auth_status,access_state', [('auth_required','pending_auth'), ('auth_failed','reconnecting')])
async def test_port_forward_process_recovers_and_identity_replacement_stops_it(tmp_path, auth_status, access_state):
    import sys
    gateway=Gateway(tmp_path)
    gateway.service=owned_service()
    executable=tmp_path/'fake-kubectl'
    executable.write_text(f'''#!{sys.executable}
import socket
listener=socket.socket();listener.bind(('127.0.0.1',0));listener.listen()
print('Forwarding from 127.0.0.1:%d -> 8001'%listener.getsockname()[1],flush=True)
while True:
    connection,_=listener.accept()
    print('Handling connection for %d'%listener.getsockname()[1],flush=True)
    connection.sendall(b'hello')
    while connection.recv(1024): pass
    connection.close()
''')
    executable.chmod(0o755);gateway.kubectl=str(executable)
    access=ServiceAccess(gateway,{'service_targets':{SERVICE:TARGET}})
    access.identities[SERVICE]='service-1'
    auth = {'required': True}
    gateway._require_auth = lambda: {'status':auth_status} if auth['required'] else None
    access.start(SERVICE)
    async def ready():
        async with asyncio.timeout(8):
            while access.status(SERVICE)['state']!='ready':await asyncio.sleep(.02)
    try:
        async with asyncio.timeout(2):
            while access.status(SERVICE)['state'] != access_state: await asyncio.sleep(.02)
        assert gateway.commands == []
        auth['required'] = False
        await ready()
        for _ in range(2):
            opened = await access.open(SimpleNamespace(input={'service_ref':SERVICE}))
            assert await asyncio.wait_for(opened.stream.read(5), 2) == b'hello'
            # Closing an unhealthy target must propagate an aborted stream.
            await opened.stream.aclose(1011)
            with pytest.raises(StreamAborted):
                await opened.stream.read(1)
            await asyncio.sleep(.05)
            assert access.status(SERVICE)['state'] == 'ready'
        first=access.channels[SERVICE]['process']
        first.kill();await first.wait()
        async with asyncio.timeout(8):
            while access.channels[SERVICE]['process'] is first or access.channels[SERVICE]['port'] is None:
                await asyncio.sleep(.02)
        assert access.status(SERVICE)['state']=='ready'
        assert access.channels[SERVICE]['process'] is not first
        # A new Pod behind the same Service can recover. Reusing the Service
        # name under a different UID is a configuration error instead.
        gateway.service=owned_service('different-service')
        second=access.channels[SERVICE]['process'];second.kill();await second.wait()
        async with asyncio.timeout(8):
            while not access.channels[SERVICE]['task'].done():await asyncio.sleep(.02)
        assert access.status(SERVICE)['state']=='failed'
        assert access.status(SERVICE)['last_error']=='service_identity_changed'
        assert access.channels[SERVICE]['port'] is None
    finally:
        await access.close()
