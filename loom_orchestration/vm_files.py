"""Verified, bounded-memory vm-file.v1 transfers through a discovered activation."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import stat
import struct
import tempfile

from .grip import GripError

PROTOCOL = "vm-file.v1"
CHUNK = 65536


class FileTransferError(RuntimeError):
    pass


def frame(value):
    data = json.dumps(value, separators=(",", ":")).encode()
    if len(data) > CHUNK:
        raise FileTransferError("transfer_frame_invalid")
    return struct.pack("!I", len(data)) + data


async def read_exact(stream, size):
    data = bytearray()
    while len(data) < size:
        chunk = await stream.read(min(CHUNK, size - len(data)))
        if not chunk:
            raise FileTransferError("transfer_incomplete")
        data.extend(chunk)
    return bytes(data)


async def read_frame(stream):
    size = struct.unpack("!I", await read_exact(stream, 4))[0]
    if not 0 < size <= CHUNK:
        raise FileTransferError("transfer_frame_invalid")
    try:
        value = json.loads(await read_exact(stream, size))
    except ValueError as exc:
        raise FileTransferError("transfer_frame_invalid") from exc
    if not isinstance(value, dict):
        raise FileTransferError("transfer_frame_invalid")
    if value.get("status") == "error":
        raise FileTransferError(value.get("error_code", "file_transfer_failed"))
    return value


def destination(path, overwrite):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISREG(info.st_mode):
        raise FileTransferError("destination_not_regular")
    if not overwrite:
        raise FileTransferError("destination_exists")


def hash_file(source):
    digest = hashlib.sha256()
    size = 0
    while chunk := source.read(CHUNK):
        size += len(chunk)
        digest.update(chunk)
    source.seek(0)
    return size, digest.hexdigest()


class VmFileClient:
    def __init__(self, client, activation, actions):
        self.client, self.activation, self.actions = client, activation, actions

    @classmethod
    async def discover(cls, client, activation):
        description = await client.describe(activation, refresh=True)
        if description["resource"]["mcue"] != "capability/activation":
            raise FileTransferError("activation_required")
        actions = {a["name"]: a for a in description["resource"]["metadata"].get("actions", [])}
        if any(name not in actions or actions[name].get("interaction") != "byte-stream"
               for name in ("files.upload", "files.download")):
            raise FileTransferError("file_actions_unavailable")
        return cls(client, activation, {name: actions[name]["uri"] for name in ("files.upload", "files.download")})

    async def open(self, action, args, timeout_seconds):
        deadline = (datetime.now(timezone.utc) + timedelta(seconds=timeout_seconds)).isoformat()
        stream = await self.client.open_stream(self.activation, self.actions["files." + action],
            {**args, "timeout_seconds": timeout_seconds}, deadline=deadline)
        if stream.body != {"protocol": PROTOCOL, "direction": action}:
            await stream.aclose()
            raise FileTransferError("transfer_protocol_invalid")
        return stream

    async def upload(self, local_path, remote_path, *, overwrite=False, timeout_seconds=300):
        fd = os.open(local_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise FileTransferError("source_not_regular")
            size, digest = await asyncio.to_thread(hash_file, source)
            args = {"path": remote_path, "size": size, "sha256": digest, "overwrite": overwrite}
            async with await self.open("upload", args, timeout_seconds) as stream:
                ready = await read_frame(stream)
                if ready != {"status": "ready", "path": remote_path, "size": size, "sha256": digest}:
                    raise FileTransferError("transfer_frame_invalid")
                remaining = size
                while remaining:
                    chunk = source.read(min(CHUNK, remaining))
                    if not chunk:
                        raise FileTransferError("source_changed")
                    await stream.write(chunk)
                    remaining -= len(chunk)
                if source.read(1):
                    raise FileTransferError("source_changed")
                await stream.write(frame({"type": "commit", "size": size, "sha256": digest}))
                result = await read_frame(stream)
                if result != {"status": "uploaded", "path": remote_path, "size": size, "sha256": digest} or await stream.read(1):
                    raise FileTransferError("file_integrity_failed")
                return result

    async def download(self, remote_path, local_path, *, overwrite=False, timeout_seconds=300):
        target = Path(local_path)
        destination(target, overwrite)
        # Existing parents are required on both sides; no unexpected directory creation.
        fd, temporary = tempfile.mkstemp(prefix=".loom-download-", dir=target.parent)
        try:
            with os.fdopen(fd, "wb") as output:
                async with await self.open("download", {"path": remote_path}, timeout_seconds) as stream:
                    header = await read_frame(stream)
                    size = header.get("size")
                    if (header.get("status") != "downloading" or header.get("path") != remote_path
                            or type(size) is not int or not 0 <= size <= 2**53 - 1):
                        raise FileTransferError("transfer_frame_invalid")
                    remaining = size
                    digest = hashlib.sha256()
                    while remaining:
                        chunk = await stream.read(min(CHUNK, remaining))
                        if not chunk:
                            raise FileTransferError("transfer_incomplete")
                        output.write(chunk)
                        digest.update(chunk)
                        remaining -= len(chunk)
                    result = await read_frame(stream)
                    if result != {"status": "downloaded", "path": remote_path, "size": size, "sha256": digest.hexdigest()} or await stream.read(1):
                        raise FileTransferError("file_integrity_failed")
                output.flush()
                os.fsync(output.fileno())
            destination(target, overwrite)
            if overwrite:
                os.replace(temporary, target)
            else:
                os.link(temporary, target)
                os.unlink(temporary)
            return result
        finally:
            Path(temporary).unlink(missing_ok=True)


def main():
    import argparse
    from .client import LoomClient

    parser = argparse.ArgumentParser(description="Upload/download files through an authenticated VM activation")
    parser.add_argument("--observer", default=os.environ.get("LOOM_OBSERVER_URL", "http://127.0.0.1:18080"))
    parser.add_argument("--token-file", type=Path)
    parser.add_argument("--activation", required=True)
    parser.add_argument("--allow-http", action="store_true")
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("upload", "download"):
        command = commands.add_parser(name)
        command.add_argument("source")
        command.add_argument("destination")
        command.add_argument("--overwrite", action="store_true")
        command.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args()

    async def run():
        token = args.token_file.read_text().strip() if args.token_file else os.environ.get("LOOM_GRIP_TOKEN")
        async with LoomClient(args.observer, token=token, allow_insecure_grip=args.allow_http) as client:
            files = await VmFileClient.discover(client, args.activation)
            return await getattr(files, args.command)(args.source, args.destination,
                overwrite=args.overwrite, timeout_seconds=args.timeout)
    try:
        print(json.dumps(asyncio.run(run()), ensure_ascii=False))
    except (FileTransferError, GripError, OSError) as exc:
        parser.exit(1, f"file transfer failed: {exc}\n")


if __name__ == "__main__":
    main()
