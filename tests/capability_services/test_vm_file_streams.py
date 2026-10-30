"""Actual WebSocket, SSH subprocess and VM helper, with disposable local SSH keys."""
import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
import os
from pathlib import Path
import pwd
import shutil
import socket
import subprocess
import time

import pytest

from capability_services.ssh_app import create_app
from capability_services.ssh_execution import GatewayError
from capability_services.ssh_files import validate_transfer
from loom_orchestration.vm_files import VmFileClient, FileTransferError, frame, read_frame
from loom_orchestration.streams import connect_stream, STREAM_PROTOCOL, StreamAborted
from tests.support.asgi_server import running_asgi


@pytest.fixture
def ssh_server(tmp_path):
    executable = shutil.which("sshd")
    if executable is None or os.geteuid() != 0 or not Path('/run/sshd').is_dir():
        pytest.skip("local sshd integration requires root and /run/sshd")
    for name in ("host", "id_ed25519"):
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(tmp_path/name)], check=True)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    config = tmp_path/'sshd.conf'
    config.write_text(f'''Port {port}
ListenAddress 127.0.0.1
HostKey {tmp_path/'host'}
AuthorizedKeysFile {tmp_path/'id_ed25519.pub'}
PidFile {tmp_path/'sshd.pid'}
PermitRootLogin yes
PasswordAuthentication no
KbdInteractiveAuthentication no
UsePAM no
StrictModes no
LogLevel ERROR
''')
    with (tmp_path/'sshd.log').open('wb') as log:
        process = subprocess.Popen([executable, '-D', '-e', '-f', str(config)], stdout=log, stderr=log)
        try:
            for _ in range(100):
                assert process.poll() is None, (tmp_path/'sshd.log').read_text()
                try:
                    with socket.create_connection(('127.0.0.1',port),timeout=.1):break
                except OSError:time.sleep(.02)
            else:pytest.fail('sshd did not start')
            yield {'host':'127.0.0.1','port':port,'user':pwd.getpwuid(os.getuid()).pw_name}
        finally:
            process.terminate();process.wait(timeout=5)


@pytest.fixture(params=['vm_ssh_http','suanli_vm_ssh_http'])
def gateway(request,tmp_path,ssh_server):
    from importlib import import_module
    module=import_module('capability_services.'+request.param+'.server')
    instance=tmp_path/'instance.json';instance.write_text(json.dumps(ssh_server))
    state=tmp_path/'state';state.mkdir()
    if request.param=='vm_ssh_http':
        gateway=module.VmSshGateway(instance_path=instance,credential_root=tmp_path,state_root=state)
    else:
        (state/'.ssh').mkdir()
        shutil.copy2(tmp_path/'id_ed25519',state/'.ssh/id_ecdsa')
        gateway=module.VmSshGateway(instance_path=instance,state_root=state)
    yield gateway
    if hasattr(gateway,'close'):gateway.close()


class DirectClient:
    """Direct public wire binding; full Observer authorization is tested separately."""
    def __init__(self,base):self.base=base
    async def open_stream(self,target,action,args,**kwargs):
        return await connect_stream(self.base.replace('http:','ws:')+'/v1/files/'+action,
            {'version':'grip.v1','request_id':'files-test','method':'INVOKE',
             'target':target,'action':'grip://test/actions/'+action,'auth':{'credential':'test'},
             'input':args,'accepted_protocols':[STREAM_PROTOCOL],**kwargs})


@asynccontextmanager
async def client(gateway):
    async with running_asgi(create_app(gateway)) as base:
        yield VmFileClient(DirectClient(base),'grip://test/activation',{'files.upload':'upload','files.download':'download'})


@pytest.mark.parametrize('size',[0,21*1024*1024+3])
async def test_real_ssh_binary_round_trip_above_exec_limit(gateway,tmp_path,size):
    source=tmp_path/'source.bin'
    # Deterministic binary includes every byte value; no whole-file buffer needed.
    with source.open('wb') as f:
        remaining=size
        while remaining:
            data=(bytes(range(256))*256)[:min(65536,remaining)];f.write(data);remaining-=len(data)
    remote=tmp_path/"remote '中文;$(printf injected).bin"
    downloaded=tmp_path/'download.bin'
    async with client(gateway) as files:
        up=await files.upload(source,str(remote))
        down=await files.download(str(remote),downloaded)
        assert up['size']==down['size']==size
        assert up['sha256']==down['sha256']==hashlib.sha256(source.read_bytes()).hexdigest()
        assert source.read_bytes()==downloaded.read_bytes()==remote.read_bytes()
        assert not (tmp_path/'injected').exists()
        assert remote.stat().st_mode & 0o777==0o600
        with pytest.raises(FileTransferError,match='destination_exists'):
            await files.upload(source,str(remote))
        with pytest.raises(FileTransferError,match='destination_exists'):
            await files.download(str(remote),downloaded)
        source.write_bytes(b'replacement\x00')
        await files.upload(source,str(remote),overwrite=True)
        await files.download(str(remote),downloaded,overwrite=True)
        assert downloaded.read_bytes()==b'replacement\x00'
    assert not list(tmp_path.glob('.loom-*'))


async def test_bad_digest_and_truncated_upload_preserve_old_file(gateway,tmp_path):
    remote=tmp_path/'existing';remote.write_bytes(b'original')
    async with client(gateway) as files:
        args={'path':str(remote),'size':4,'sha256':'0'*64,'overwrite':True}
        async with await files.open('upload',args,30) as stream:
            assert (await read_frame(stream))['status']=='ready'
            await stream.write(b'bad!'+frame({'type':'commit','size':4,'sha256':'0'*64}))
            with pytest.raises(FileTransferError,match='file_integrity_failed'):
                await read_frame(stream)
        assert remote.read_bytes()==b'original'
        async with await files.open('upload',args,30) as stream:
            assert (await read_frame(stream))['status']=='ready'
            await stream.write(b'extra'+frame({'type':'commit','size':4,'sha256':'0'*64}))
            with pytest.raises(FileTransferError,match='transfer_frame_invalid'):
                await read_frame(stream)
        assert remote.read_bytes()==b'original'
        async with await files.open('upload',{**args,'size':100000},30) as stream:
            assert (await read_frame(stream))['status']=='ready'
            await stream.write(b'partial')
            # Wait until the SSH process creates its staging file before disconnect.
            for _ in range(100):
                if list(tmp_path.glob('.loom-upload-*')):break
                await asyncio.sleep(.02)
            assert list(tmp_path.glob('.loom-upload-*'))
        for _ in range(200):
            if not list(tmp_path.glob('.loom-upload-*')):break
            await asyncio.sleep(.02)
        assert not list(tmp_path.glob('.loom-upload-*'))
        assert remote.read_bytes()==b'original'


async def test_concurrent_no_overwrite_has_one_atomic_winner(gateway,tmp_path):
    remote=tmp_path/'race'
    async with client(gateway) as files:
        data=[b'first',b'second']
        args=[{'path':str(remote),'size':len(x),'sha256':hashlib.sha256(x).hexdigest()} for x in data]
        streams=[await files.open('upload',a,30) for a in args]
        try:
            for stream in streams:assert (await read_frame(stream))['status']=='ready'
            results=[]
            for stream,a,body in zip(streams,args,data):
                await stream.write(body+frame({'type':'commit','size':a['size'],'sha256':a['sha256']}))
                try:results.append(await read_frame(stream))
                except FileTransferError as exc:results.append(str(exc))
            assert results[0]['status']=='uploaded' and results[1]=='destination_exists'
            assert remote.read_bytes()==data[0]
        finally:
            for stream in streams:await stream.aclose()
    assert not list(tmp_path.glob('.loom-upload-*'))


def test_remote_download_detects_source_change(tmp_path):
    import io
    from capability_services.ssh_files_remote import download
    source=tmp_path/'changing';source.write_bytes(b'original')
    class ChangingOutput(io.BytesIO):
        def write(self,data):
            if not self.tell():
                with source.open('ab') as f:f.write(b'appended')
            return super().write(data)
    with pytest.raises(ValueError,match='source_changed'):
        download({'path':str(source)},ChangingOutput())


async def test_symlinks_missing_files_and_timeout(gateway,tmp_path):
    source=tmp_path/'source';source.write_bytes(b'ok')
    link=tmp_path/'link';link.symlink_to(source)
    async with client(gateway) as files:
        with pytest.raises(FileTransferError,match='destination_not_regular'):
            await files.upload(source,str(link),overwrite=True)
        for path in [link,tmp_path/'missing']:
            with pytest.raises(FileTransferError):
                await files.download(str(path),tmp_path/'download')
            assert not (tmp_path/'download').exists()
        stream=await files.open('upload',{'path':str(tmp_path/'timeout'),'size':1,'sha256':'0'*64},1)
        with pytest.raises((StreamAborted,FileTransferError)):
            async with stream:
                assert (await read_frame(stream))['status']=='ready'
                await read_frame(stream)
        assert not (tmp_path/'timeout').exists()
    assert source.read_bytes()==b'ok'


async def test_truncated_or_corrupt_download_does_not_replace_local_file(tmp_path):
    class Stream:
        body={'protocol':'vm-file.v1','direction':'download'}
        def __init__(self,payload):self.data=payload
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def read(self,n):data,self.data=self.data[:n],self.data[n:];return data
    class Transport:
        async def open_stream(self,*args,**kwargs):return Stream(payload)
    files=VmFileClient(Transport(),'grip://test/a',{'files.download':'download'})
    target=tmp_path/'keep';target.write_bytes(b'original')
    header=frame({'status':'downloading','path':'/remote','size':4})
    for payload in [header+b'ab',header+b'abcd'+frame({'status':'downloaded','path':'/remote','size':4,'sha256':'0'*64})]:
        with pytest.raises(FileTransferError):await files.download('/remote',target,overwrite=True)
        assert target.read_bytes()==b'original'
        assert not list(tmp_path.glob('.loom-download-*'))


@pytest.mark.parametrize('arguments',[{'path':'relative'},{'path':'/x\x00'}, {'path':'/x','host':'evil'},
    {'path':'/x','timeout_seconds':True},{'path':'/x','timeout_seconds':float('nan')}])
def test_transfer_admission_rejects_invalid_paths_and_target_override(arguments):
    with pytest.raises(GatewayError):validate_transfer('download',arguments)
