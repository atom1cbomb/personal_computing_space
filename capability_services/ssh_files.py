"""Bounded SSH pipe transport; credentials and target come from the activation."""
import asyncio
from contextlib import suppress
import json
import math
import os
from pathlib import Path
import re
import shlex
import signal

from loom_orchestration.streams import StreamAborted
from .ssh_execution import GatewayError

PROTOCOL = "vm-file.v1"
CHUNK = 65536


def validate_transfer(action, args):
    fields = {"path", "timeout_seconds"} | ({"size", "sha256", "overwrite"} if action == "upload" else set())
    if action not in {"upload", "download"} or not isinstance(args, dict) or set(args) - fields:
        raise GatewayError("invalid_input")
    path = args.get("path")
    if not isinstance(path, str) or not path.startswith("/") or "\x00" in path or len(path) > 4096:
        raise GatewayError("invalid_path")
    timeout = args.get("timeout_seconds", 300)
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 1 <= timeout <= 3600:
        raise GatewayError("timeout_invalid")
    if action == "upload":
        if type(args.get("size")) is not int or not 0 <= args["size"] <= 2**53 - 1:
            raise GatewayError("file_size_invalid")
        if not isinstance(args.get("sha256"), str) or not re.fullmatch("[0-9a-f]{64}", args["sha256"]):
            raise GatewayError("file_digest_invalid")
        if type(args.get("overwrite", False)) is not bool:
            raise GatewayError("invalid_input")
    return float(timeout)


class SSHFileStream:
    def __init__(self, process, action):
        self.process, self.action = process, action
        self.closed = False
        self.stderr = asyncio.create_task(self._drain_errors())

    @classmethod
    async def open(cls, ssh_args, action, args):
        source = Path(__file__).with_name("ssh_files_remote.py").read_text()
        command = shlex.join(["python3", "-u", "-c", source, action, json.dumps(args)])
        process = await asyncio.create_subprocess_exec(*ssh_args, command,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            limit=CHUNK, start_new_session=True)
        return cls(process, action)

    async def _drain_errors(self):
        # Never return provider diagnostics (which may contain credential paths).
        while await self.process.stderr.read(CHUNK):
            pass

    async def read(self, max_bytes=CHUNK):
        data = await self.process.stdout.read(min(max_bytes, CHUNK))
        if not data and await self.process.wait() != 0:
            raise StreamAborted()
        return data

    async def write(self, data):
        if self.action != "upload" or self.closed:
            raise StreamAborted(1002)
        try:
            self.process.stdin.write(data)
            await self.process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise StreamAborted() from exc

    async def aclose(self, code=1000):
        if self.closed:
            return
        self.closed = True
        self.process.stdin.close()
        # EOF lets a partial upload clean up its temporary file on the VM.
        try:
            await asyncio.wait_for(self.process.wait(), 3)
        except asyncio.TimeoutError:
            with suppress(ProcessLookupError):
                os.killpg(self.process.pid, signal.SIGKILL)
            await self.process.wait()
        await self.stderr
