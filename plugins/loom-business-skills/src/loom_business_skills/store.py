from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import tempfile

from .model import Problem, encoded, markdown


def private_write(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(text)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def terms(text):
    words = re.findall(r"[a-z0-9_-]+", text.lower())
    for segment in re.findall(r"[\u4e00-\u9fff]+", text):
        words.extend(segment[i:i + 2] for i in range(max(1, len(segment) - 1)))
    return set(words)


class Store:
    def __init__(self, root=None):
        self.root = Path(root or "~/.local/share/loom-business-skills").expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.root.stat().st_mode & 0o077:
            raise Problem("cache_directory_not_private")
        self.path = self.root / "index.sqlite3"
        # SQLite also holds the full record; generated Markdown is a disposable view.
        fd = os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600)
        os.fchmod(fd, 0o600)
        os.close(fd)
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS experiences (id TEXT PRIMARY KEY, summary TEXT NOT NULL, record TEXT NOT NULL, revision INTEGER NOT NULL)")

    def connect(self):
        return sqlite3.connect(self.path)

    @staticmethod
    def validate_id(key):
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", key):
            raise Problem("invalid_experience_id")

    def get(self, key):
        self.validate_id(key)
        with self.connect() as db:
            row = db.execute("SELECT record, revision FROM experiences WHERE id=?", (key,)).fetchone()
        if row is None:
            raise Problem("experience_not_found")
        return {**json.loads(row[0]), "_revision": row[1]}

    def put(self, record):
        record = dict(record)
        revision = record.pop("_revision", None)
        d = record["draft"]
        self.validate_id(d["id"])
        summary = {k: d[k] for k in ("id", "title", "summary", "keywords")}
        summary.update(status=record.get("last_check", {}).get("status", "unchecked"),
                       verified_steps=sum(e.get("level") == "verified" for e in record["evidence"].values()))
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT revision FROM experiences WHERE id=?", (d["id"],)).fetchone()
            if (existing[0] if existing else None) != revision:
                raise Problem("experience_changed" if revision is not None else "experience_exists")
            new_revision = (revision or 0) + 1
            db.execute("INSERT INTO experiences VALUES (?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET summary=excluded.summary, record=excluded.record, revision=excluded.revision",
                       (d["id"], encoded(summary), encoded(record), new_revision))
        # Read-only views never grant permission or provide a preflight result.
        private_write(self.root / "experiences" / d["id"] / "SKILL.md", markdown(record))
        return {**record, "_revision": new_revision}

    def find(self, query, limit=3):
        if not 1 <= limit <= 5 or len(query) > 240:
            raise Problem("search_budget_invalid")
        wanted = terms(query)
        matches = []
        with self.connect() as db:
            for (value,) in db.execute("SELECT summary FROM experiences"):
                summary = json.loads(value)
                text = " ".join([summary["title"], summary["summary"], *summary["keywords"]])
                score = len(wanted & terms(text))
                if score or not wanted:
                    matches.append((score, summary))
        matches.sort(key=lambda pair: (-pair[0], pair[1]["id"]))
        return [{k: v for k, v in row.items() if k != "keywords"} for _, row in matches[:limit]]

    def remove(self, key):
        self.validate_id(key)
        with self.connect() as db:
            db.execute("DELETE FROM experiences WHERE id=?", (key,))
        path = self.root / "experiences" / key
        if path.is_dir():
            shutil.rmtree(path)
