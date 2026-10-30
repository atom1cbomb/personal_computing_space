"""Limits, source archives and the durable operation journal."""
from __future__ import annotations

import hashlib
from contextlib import contextmanager
import io
import json
from pathlib import Path
import sqlite3
import stat
import threading
import zipfile

ORIGIN = "https://overleaf.cs.ac.cn"
MAX_ARCHIVE = 50 * 1024 * 1024
MAX_EXPANDED = 64 * 1024 * 1024
MAX_FILES = 2000


class OverleafError(Exception):
    def __init__(self, code, status=400, **details):
        super().__init__(code)
        self.code, self.status, self.details = code, status, details

    def result(self):
        return {"status": "error", "error_code": self.code, **self.details}


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def source_path(name: str) -> str:
    if (not isinstance(name, str) or not name or len(name.encode()) > 1024
            or "\\" in name or any(ord(c) < 32 or ord(c) == 127 for c in name)
            or any(p in {"", ".", ".."} or p.lower() == ".git" or ":" in p for p in name.split("/"))):
        raise OverleafError("unsafe_source_path")
    return name


def unpack(data: bytes, *, envelope=False) -> dict[str, bytes]:
    if len(data) > MAX_ARCHIVE:
        raise OverleafError("archive_too_large")
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            infos = archive.infolist()
            if (len(infos) > MAX_FILES + int(envelope)
                    or sum(i.file_size for i in infos) > MAX_EXPANDED + (4 * 1024 * 1024 if envelope else 0)):
                raise OverleafError("archive_limits_exceeded")
            files, seen = {}, set()
            for item in infos:
                name = source_path(item.filename.rstrip("/") if item.is_dir() else item.filename)
                mode = stat.S_IFMT(item.external_attr >> 16)
                if name in seen or mode not in (0, stat.S_IFREG, stat.S_IFDIR):
                    raise OverleafError("unsafe_archive_entry")
                seen.add(name)
                if not item.is_dir():
                    files[name] = archive.read(item)
            # A file and its descendant cannot both be materialized safely.
            for name in files:
                if any("/".join(name.split("/")[:i]) in files for i in range(1, len(name.split("/")))):
                    raise OverleafError("unsafe_archive_entry")
            return files
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError, EOFError, ValueError) as exc:
        raise OverleafError("invalid_zip") from exc


def pack(files: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(files.items()):
            # Fixed timestamps make retries from unchanged files byte-identical.
            item = zipfile.ZipInfo(source_path(name), date_time=(1980, 1, 1, 0, 0, 0))
            item.compress_type = zipfile.ZIP_DEFLATED
            item.external_attr = (stat.S_IFREG | 0o644) << 16
            archive.writestr(item, data)
    value = output.getvalue()
    if len(value) > MAX_ARCHIVE:
        raise OverleafError("archive_too_large")
    return value


def manifest(files):
    return {name: {"sha256": digest(data), "size": len(data)} for name, data in sorted(files.items())}


class Journal:
    """Persist before a side effect; a restart never turns sending into unsent."""
    def __init__(self, root: Path):
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = root / "operations.sqlite3"
        self.lock = threading.RLock()
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS operations (key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, value TEXT NOT NULL)")
        self.path.chmod(0o600)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        try:
            db.execute("PRAGMA synchronous=FULL")
            with db:
                yield db
        finally:
            db.close()

    def get(self, key, fingerprint):
        with self.lock, self.connect() as db:
            row = db.execute("SELECT fingerprint,value FROM operations WHERE key=?", (key,)).fetchone()
            if row and row[0] != fingerprint:
                raise OverleafError("idempotency_conflict", 409)
            return json.loads(row[1]) if row else None

    def put(self, key, fingerprint, value):
        with self.lock, self.connect() as db:
            db.execute("INSERT INTO operations VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                       (key, fingerprint, json.dumps(value, sort_keys=True)))


def fingerprint(arguments):
    return digest(json.dumps(arguments, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode())
