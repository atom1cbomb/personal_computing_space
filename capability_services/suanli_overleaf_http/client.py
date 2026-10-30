"""External-agent helpers. Local paths are never sent to the service."""
from __future__ import annotations

import fnmatch
import json
import os
from pathlib import Path
import struct
import tempfile

from .common import MAX_ARCHIVE, MAX_EXPANDED, MAX_FILES, OverleafError, digest, manifest, pack, source_path, unpack

PROTOCOL = "overleaf-archive.v1"
DEFAULT_EXCLUDES = (".git", "*.aux", "*.log", "*.toc", "*.out", "*.fls", "*.fdb_latexmk",
                    "*.synctex.gz", "*.blg", "*.nav", "*.snm", "*.vrb", "*.xdv")


async def read_exact(stream, size):
    data = bytearray()
    while len(data) < size:
        chunk = await stream.read(min(65536, size-len(data)))
        if not chunk:
            raise OverleafError("transfer_incomplete")
        data.extend(chunk)
    return bytes(data)


async def read_result(stream):
    length = struct.unpack("!I", await read_exact(stream, 4))[0]
    if not 0 < length <= 65536:
        raise OverleafError("transfer_result_invalid")
    try:
        value = json.loads(await read_exact(stream, length))
    except ValueError as exc:
        raise OverleafError("transfer_result_invalid") from exc
    if not isinstance(value, dict) or not isinstance(value.get("status"), str):
        raise OverleafError("transfer_result_invalid")
    if value["status"] == "error":
        raise OverleafError(value.get("error_code", "transfer_failed"))
    return value


def local_files(directory, *, tracked=(), exclude=()):
    root = Path(directory)
    if root.is_symlink() or not root.is_dir():
        raise OverleafError("local_directory_invalid")
    result, total = {}, 0
    for current, directories, filenames in os.walk(root, followlinks=False):
        directories[:] = [n for n in directories if n != ".git"]
        for name in directories:
            if (Path(current) / name).is_symlink():
                raise OverleafError("local_symlink_not_supported")
        for name in filenames:
            path = Path(current) / name
            relative = source_path(path.relative_to(root).as_posix())
            if relative not in tracked and any(fnmatch.fnmatch(relative, pattern) or fnmatch.fnmatch(name, pattern)
                                               for pattern in (*DEFAULT_EXCLUDES, *exclude)):
                continue
            if path.is_symlink() or not path.is_file():
                raise OverleafError("local_special_file_not_supported")
            total += path.stat().st_size
            if total > MAX_EXPANDED or len(result) >= MAX_FILES:
                raise OverleafError("project_limits_exceeded")
            result[relative] = path.read_bytes()
    return result


def save_state(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".overleaf-state-")
    try:
        with os.fdopen(fd, "w") as output:
            json.dump(value, output, ensure_ascii=False, sort_keys=True, indent=2)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


class OverleafClient:
    def __init__(self, client, activation, actions):
        self.client, self.activation, self.actions = client, activation, actions

    @classmethod
    async def discover(cls, client, activation):
        description = await client.describe(activation)
        actions = {row["name"]: row["uri"] for row in description["resource"]["metadata"]["actions"]}
        if not all("projects." + name in actions for name in ("list", "download", "status", "upload", "import")):
            raise OverleafError("overleaf_actions_unavailable")
        return cls(client, activation, actions)

    async def list(self, *, query="", offset=0, limit=100):
        return await self.query("list", {"query": query, "offset": offset, "limit": limit})

    async def query(self, action, args):
        value = await self.client.invoke(self.activation, self.actions["projects."+action], args)
        if value["status"] == "error":
            raise OverleafError(value["error_code"])
        return value["result"]

    async def status(self, state_file):
        state = self.state(state_file)
        return await self.query("status", {"project_id": state["project_id"], "base_revision": state["revision"]})

    def state(self, path):
        value = json.loads(Path(path).read_text())
        if value.get("activation") != self.activation:
            raise OverleafError("activation_mismatch")
        return value

    @staticmethod
    def outside(directory, state_file):
        if Path(state_file).resolve().is_relative_to(Path(directory).resolve()):
            raise OverleafError("state_must_be_outside_project")

    async def open(self, action, args):
        stream = await self.client.open_stream(self.activation, self.actions["projects."+action], args)
        direction = "download" if action == "download" else "upload"
        if stream.body != {"protocol": PROTOCOL, "direction": direction}:
            await stream.aclose()
            raise OverleafError("transfer_protocol_invalid")
        return stream

    async def download(self, project_id, directory, state_file):
        self.outside(directory, state_file)
        directory = Path(directory)
        if directory.exists() or directory.is_symlink() or Path(state_file).exists():
            raise OverleafError("local_destination_exists")
        async with await self.open("download", {"project_id": project_id}) as stream:
            result = await read_result(stream)
            size = result.get("archive_size")
            if result["status"] != "downloaded" or type(size) is not int or not 0 < size <= MAX_ARCHIVE:
                raise OverleafError("transfer_result_invalid")
            archive = await read_exact(stream, size)
            if digest(archive) != result.get("archive_sha256") or await stream.read(1):
                raise OverleafError("archive_integrity_failed")
        envelope = unpack(archive, envelope=True)
        try:
            metadata = json.loads(envelope.pop("manifest.json"))
            if any(not n.startswith("project/") for n in envelope):
                raise ValueError()
            files = {source_path(n.removeprefix("project/")): value for n, value in envelope.items()}
            if (metadata["project_id"] != project_id or result["project_id"] != project_id
                    or metadata["revision"] != result["revision"] or metadata["files"] != manifest(files)):
                raise ValueError()
        except (ValueError, KeyError, TypeError) as exc:
            raise OverleafError("download_manifest_invalid") from exc
        directory.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=directory.parent, prefix=".overleaf-download-") as temporary:
            staging = Path(temporary) / "project"
            staging.mkdir()
            for name, value in files.items():
                destination = staging / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(value)
            # Never unpack into a user's existing working directory.
            staging.rename(directory)
        save_state(state_file, {"activation": self.activation, **metadata})
        return result

    async def send(self, action, arguments, archive):
        args = {**arguments, "archive_size": len(archive), "archive_sha256": digest(archive)}
        async with await self.open(action, args) as stream:
            await stream.write(archive)
            result = await read_result(stream)
            if await stream.read(1):
                raise OverleafError("transfer_result_invalid")
            return result

    async def upload(self, directory, state_file, *, message, idempotency_key, exclude=()):
        self.outside(directory, state_file)
        state = self.state(state_file)
        files = local_files(directory, tracked=state["files"], exclude=exclude)
        current = manifest(files)
        previous = state.get("last_upload")
        if previous and previous["idempotency_key"] == idempotency_key:
            if current != state["files"] or message != previous["message"]:
                raise OverleafError("idempotency_conflict")
            return previous["result"]
        changed = {name: value for name, value in files.items() if state["files"].get(name) != current[name]}
        deleted = sorted(set(state["files"]) - set(files))
        result = await self.send("upload", {"project_id": state["project_id"], "base_revision": state["revision"],
            "deleted_paths": deleted, "message": message, "idempotency_key": idempotency_key}, pack(changed))
        if result["status"] in ("uploaded", "unchanged"):
            if result.get("project_id") != state["project_id"]:
                raise OverleafError("transfer_result_invalid")
            save_state(state_file, {**state, "revision": result["revision"], "files": current,
                "last_upload": {"idempotency_key": idempotency_key, "message": message, "result": result}})
        return result

    async def import_project(self, directory, *, name, idempotency_key, exclude=()):
        return await self.send("import", {"name": name, "idempotency_key": idempotency_key}, pack(local_files(directory, exclude=exclude)))

    async def import_zip(self, archive_path, *, name, idempotency_key):
        path = Path(archive_path)
        if path.stat().st_size > MAX_ARCHIVE:
            raise OverleafError("archive_too_large")
        archive = path.read_bytes()
        unpack(archive)
        return await self.send("import", {"name": name, "idempotency_key": idempotency_key}, archive)


def main():
    import argparse
    import asyncio
    from loom_orchestration import LoomClient

    parser = argparse.ArgumentParser(description="Suanli Overleaf projects through a discovered Loom activation")
    parser.add_argument("--observer", default=os.environ.get("LOOM_OBSERVER_URL", "http://127.0.0.1:18080"))
    parser.add_argument("--activation", required=True)
    parser.add_argument("--allow-http", action="store_true")
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list")
    listing.add_argument("--query", default="")
    listing.add_argument("--offset", type=int, default=0)
    listing.add_argument("--limit", type=int, default=100)
    download = commands.add_parser("download")
    download.add_argument("project_id")
    download.add_argument("directory", type=Path)
    download.add_argument("--state", type=Path, required=True)
    status = commands.add_parser("status")
    status.add_argument("--state", type=Path, required=True)
    upload = commands.add_parser("upload")
    upload.add_argument("directory", type=Path)
    upload.add_argument("--state", type=Path, required=True)
    upload.add_argument("--message", required=True)
    imported = commands.add_parser("import")
    imported.add_argument("source", type=Path, help="Source directory or ZIP file")
    imported.add_argument("--name", required=True)
    for sub in (upload, imported):
        sub.add_argument("--key", required=True, help="Stable key for one operation; preserve on retries")
        sub.add_argument("--exclude", action="append", default=[])
    args = parser.parse_args()

    async def execute():
        async with LoomClient(args.observer, token=os.environ["LOOM_GRIP_TOKEN"], allow_insecure_grip=args.allow_http) as sdk:
            client = await OverleafClient.discover(sdk, args.activation)
            if args.command == "list":
                return await client.list(query=args.query, offset=args.offset, limit=args.limit)
            if args.command == "download":
                return await client.download(args.project_id, args.directory, args.state)
            if args.command == "status":
                return await client.status(args.state)
            if args.command == "upload":
                return await client.upload(args.directory, args.state, message=args.message, idempotency_key=args.key, exclude=args.exclude)
            if args.source.is_file():
                return await client.import_zip(args.source, name=args.name, idempotency_key=args.key)
            return await client.import_project(args.source, name=args.name, idempotency_key=args.key, exclude=args.exclude)

    try:
        print(json.dumps(asyncio.run(execute()), ensure_ascii=False, indent=2))
    except OverleafError as exc:
        print(json.dumps(exc.result()))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
