import asyncio
import json
import time
from datetime import datetime, timezone

import httpx
import pytest
from fastapi import FastAPI, WebSocket

from loom_orchestration import GripError, LoomClient, Names
from loom_orchestration.bindings import AccessCredential
from loom_orchestration.stream_server import OpenedStream, serve_stream
from loom_orchestration.streams import STREAM_PROTOCOL, TCPStream
from tests.support.asgi_server import running_asgi

N = Names()
TARGET = N.name("resources", "standalone")
ACTION = N.action("standalone-echo")
INPUT = N.schema("standalone-echo-input")
OUTPUT = N.schema("standalone-echo-output")
AUDIENCE = N.name("resources", "standalone-gateway")


def discovery(binding, *, mode="request-reply", expires=False):
    description = {
        "resource": {"uri": TARGET, "mcue": "data/service", "metadata": {}},
        "offers": [{"action": {"uri": ACTION, "mode": mode, "input_schema": INPUT, "output_schema": OUTPUT}, "bindings": [binding]}],
    }
    if expires:
        description["expires_at"] = datetime.fromtimestamp(time.time() + 60, timezone.utc).isoformat()
    return description


def observer_transport(description, counts):
    def handler(request):
        data = json.loads(request.content)
        counts[data["method"]] = counts.get(data["method"], 0) + 1
        assert data["auth"]["credential"] == "observer-secret"
        assert request.url.host == "observer"
        if data["method"] == "DESCRIBE":
            target = data["target"]
            if target == TARGET:
                body = description
            else:
                body = {"resource": {"uri": target, "mcue": "grip/schema"}, "offers": [{
                    "action": {"uri": N.action("read"), "mode": "request-reply", "input_schema": INPUT, "output_schema": OUTPUT},
                    "bindings": [{"protocol": "loom-http-json.v1", "endpoint": "http://observer/.well-known/grip", "audience": N.root}]}]}
        else:
            body = {"type": "object"}
        return httpx.Response(200, json={"version": "grip.v1", "request_id": data["request_id"], "kind": "reply", "body": body})
    return httpx.MockTransport(handler)


async def provider(binding, *_):
    return AccessCredential("service-only" if binding.audience == AUDIENCE else "observer-secret")


async def test_standalone_binding_scopes_auth_and_caches_contracts():
    binding = {"protocol": "https-json.v1", "endpoint": "https://service.example/invoke", "audience": AUDIENCE}
    counts, observed = {}, []
    discovery_transport = observer_transport(discovery(binding, expires=True), counts)
    async def route(request):
        if request.url.host == "observer":
            return await discovery_transport.handle_async_request(request)
        data = json.loads(request.content)
        observed.append(data)
        assert data["auth"]["credential"] == "service-only"
        assert "authorization" not in request.headers
        assert "cookie" not in request.headers
        assert "x-api-key" not in request.headers
        return httpx.Response(200, json={"version": "grip.v1", "request_id": data["request_id"], "kind": "reply", "body": {"value": data["input"]["value"]}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(route), headers={"Authorization": "Bearer observer-secret", "Cookie": "secret=value", "X-API-Key": "private"}) as transport:
        async with LoomClient("http://observer", token="observer-secret", client=transport, allow_insecure_grip=True,
                              trusted_bindings=[binding], credential_provider=provider) as client:
            assert await client.invoke(TARGET, ACTION, {"value": 1}) == {"value": 1}
            before = dict(counts)
            assert await client.invoke(TARGET, ACTION, {"value": 2}) == {"value": 2}
            assert counts == before
    assert len(observed) == 2


async def test_untrusted_binding_fails_before_credential_delivery():
    binding = {"protocol": "https-json.v1", "endpoint": "https://untrusted.example/grip", "audience": AUDIENCE}
    counts = {}
    async def forbidden_provider(*_):
        pytest.fail("credentials must not be requested before trust validation")
    async with LoomClient("http://observer", token="observer-secret", allow_insecure_grip=True,
                          transport=observer_transport(discovery(binding), counts), credential_provider=forbidden_provider) as client:
        with pytest.raises(GripError, match="untrusted"):
            await client.invoke(TARGET, ACTION, {})
    assert counts == {"DESCRIBE": 1}


async def test_real_stream_to_service_without_slave_identity():
    async def echo(reader, writer):
        try:
            while data := await reader.read(4096):
                writer.write(data)
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
    tcp = await asyncio.start_server(echo, "127.0.0.1", 0)
    port = tcp.sockets[0].getsockname()[1]
    app, calls = FastAPI(), []
    async def opener(request):
        assert request.auth.credential == "service-only"
        assert request.target == TARGET and request.action == ACTION
        calls.append(request.request_id)
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        return OpenedStream(TCPStream(reader, writer), {}, time.time() + 30)
    @app.websocket("/stream")
    async def stream(ws: WebSocket):
        await serve_stream(ws, opener)
    try:
        async with running_asgi(app) as base:
            binding = {"protocol": STREAM_PROTOCOL, "endpoint": base.replace("http:", "ws:") + "/stream", "audience": AUDIENCE}
            async with LoomClient("http://observer", token="observer-secret", allow_insecure_grip=True,
                                  transport=observer_transport(discovery(binding, mode="byte-stream"), {}),
                                  trusted_bindings=[binding], credential_provider=provider) as client:
                async with await client.open_stream(TARGET, ACTION, {}) as stream:
                    payload = bytes(range(256)) * 1024
                    async def receive():
                        result = bytearray()
                        while len(result) < len(payload):
                            result.extend(await stream.read(3000))
                        return bytes(result)
                    receiver = asyncio.create_task(receive())
                    await stream.write(payload)
                    assert await asyncio.wait_for(receiver, 10) == payload
                    with pytest.raises(GripError, match="unsupported_binding"):
                        await stream.write_eof()
                assert len(calls) == 1
    finally:
        tcp.close()
        await tcp.wait_closed()

async def test_wss_certificate_validation_and_grant_expiry(tmp_path):
    import ssl,ipaddress
    from datetime import timedelta
    from cryptography import x509
    from cryptography.x509.oid import NameOID
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from loom_orchestration.streams import StreamAborted, connect_stream
    key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    name=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'localhost')])
    now=datetime.now(timezone.utc)
    cert=(x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key()).serial_number(x509.random_serial_number())
          .not_valid_before(now-timedelta(minutes=1)).not_valid_after(now+timedelta(hours=1))
          .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]),False).sign(key,hashes.SHA256()))
    private,public=tmp_path/'key.pem',tmp_path/'cert.pem'
    private.write_bytes(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
    public.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    context=ssl.create_default_context(cafile=str(public))
    app=FastAPI();closed=[]
    class Idle:
        async def read(self,n):await asyncio.Event().wait()
        async def write(self,data):pass
        async def aclose(self,code=1000):closed.append(code)
    async def opener(request):return OpenedStream(Idle(),{},time.time()+.25)
    @app.websocket('/stream')
    async def websocket(ws:WebSocket):await serve_stream(ws,opener)
    async with running_asgi(app,ssl_keyfile=str(private),ssl_certfile=str(public)) as base:
        request={'version':'grip.v1','request_id':'tls-stream','method':'INVOKE','auth':{'credential':'test'},'target':TARGET,
                 'action':ACTION,'input':{},'accepted_protocols':[STREAM_PROTOCOL]}
        async with await connect_stream(base.replace('https:','wss:')+'/stream',request,ssl=context) as stream:
            with pytest.raises(StreamAborted) as exc:
                await asyncio.wait_for(stream.read(),2)
            assert exc.value.close_code==1008
    assert 1008 in closed
