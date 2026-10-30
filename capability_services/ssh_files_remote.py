"""Standalone Python 3 program sent over the activation's authenticated SSH.

Only stdlib is required on the VM. File bytes never enter command arguments.
"""
import hashlib
import json
import os
import stat
import struct
import sys
import tempfile

CHUNK = 65536


def frame(value):
    raw = json.dumps(value, separators=(",", ":")).encode()
    return struct.pack("!I", len(raw)) + raw


def exact(source, size):
    data = bytearray()
    while len(data) < size:
        chunk = source.read(size - len(data))
        if not chunk:
            raise ValueError("transfer_incomplete")
        data.extend(chunk)
    return bytes(data)


def read_frame(source):
    size = struct.unpack("!I", exact(source, 4))[0]
    if not 0 < size <= CHUNK:
        raise ValueError("transfer_frame_invalid")
    return json.loads(exact(source, size))


def destination(path, overwrite):
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("destination_not_regular")
    if not overwrite:
        raise ValueError("destination_exists")


def upload(args, source, output):
    path, size = args["path"], args["size"]
    overwrite = args.get("overwrite", False)
    destination(path, overwrite)
    fd, temporary = tempfile.mkstemp(prefix=".loom-upload-", dir=os.path.dirname(path))
    try:
        output.write(frame({"status": "ready", "path": path, "size": size, "sha256": args["sha256"]}))
        output.flush()
        digest = hashlib.sha256()
        with os.fdopen(fd, "wb") as file:
            remaining = size
            while remaining:
                chunk = source.read(min(CHUNK, remaining))
                if not chunk:
                    raise ValueError("transfer_incomplete")
                file.write(chunk)
                digest.update(chunk)
                remaining -= len(chunk)
            commit = read_frame(source)
            expected = {"type": "commit", "size": size, "sha256": args["sha256"]}
            if commit != expected or digest.hexdigest() != args["sha256"]:
                raise ValueError("file_integrity_failed")
            file.flush()
            os.fsync(file.fileno())
        destination(path, overwrite)
        if overwrite:
            os.replace(temporary, path)
        else:
            # Atomic create-if-absent, including another uploader winning the race.
            os.link(temporary, path)
            os.unlink(temporary)
        output.write(frame({"status": "uploaded", "path": path, "size": size, "sha256": digest.hexdigest()}))
        output.flush()
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def download(args, output):
    fd = os.open(args["path"], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as source:
        before = os.fstat(source.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("source_not_regular")
        output.write(frame({"status": "downloading", "path": args["path"], "size": before.st_size}))
        output.flush()
        remaining = before.st_size
        digest = hashlib.sha256()
        while remaining:
            chunk = source.read(min(CHUNK, remaining))
            if not chunk:
                raise ValueError("source_changed")
            digest.update(chunk)
            output.write(chunk)
            remaining -= len(chunk)
        after = os.fstat(source.fileno())
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
            raise ValueError("source_changed")
        output.write(frame({"status": "downloaded", "path": args["path"], "size": before.st_size, "sha256": digest.hexdigest()}))
        output.flush()


def main():
    try:
        args = json.loads(sys.argv[2])
        if sys.argv[1] == "upload":
            upload(args, sys.stdin.buffer, sys.stdout.buffer)
        elif sys.argv[1] == "download":
            download(args, sys.stdout.buffer)
        else:
            raise ValueError("action_not_found")
    except (OSError, ValueError) as exc:
        code = ("destination_exists" if isinstance(exc, FileExistsError) else
                "file_not_found" if isinstance(exc, FileNotFoundError) else
                "permission_denied" if isinstance(exc, PermissionError) else
                "file_operation_failed" if isinstance(exc, OSError) else str(exc))
        sys.stdout.buffer.write(frame({"status": "error", "error_code": code}))
        sys.stdout.buffer.flush()


if __name__ == "__main__":
    main()
