"""Git snapshots and optimistic, non-forced updates. Tokens never enter URLs."""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import re
import subprocess
import threading

from .common import (MAX_EXPANDED, MAX_FILES, ORIGIN, Journal, OverleafError,
                     digest, fingerprint, manifest, pack, source_path, unpack)


class GitStore:
    def __init__(self, root: Path, token_file: Path, journal: Journal, *, timeout=120):
        self.root, self.token_file, self.journal, self.timeout = root, token_file, journal, timeout
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._locks = {}
        self._lock = threading.Lock()

    def lock(self, project_id):
        with self._lock:
            return self._locks.setdefault(project_id, threading.RLock())

    def remote(self, project_id):
        return f"{ORIGIN}/git/{project_id}"

    def run(self, repo, *args, check=True, data=None):
        # A deliberately small environment excludes inherited Git config, tracing,
        # proxy credentials and local credential helpers. Askpass reads the mount.
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LC_ALL": "C",
               "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
               "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": str(Path(__file__).with_name("askpass.sh")),
               "LOOM_OVERLEAF_TOKEN_FILE": str(self.token_file),
               "GIT_AUTHOR_NAME": "Loom Overleaf", "GIT_AUTHOR_EMAIL": "loom@localhost",
               "GIT_COMMITTER_NAME": "Loom Overleaf", "GIT_COMMITTER_EMAIL": "loom@localhost"}
        try:
            result = subprocess.run(["git", "-c", "credential.helper=", "-c", "http.followRedirects=false",
                "-c", "core.hooksPath=/dev/null", "-c", "protocol.allow=never",
                "-c", "protocol.https.allow=always", "-c", "gc.auto=0", "-C", str(repo), *args],
                input=data, capture_output=True, env=env, timeout=self.timeout)
        except subprocess.TimeoutExpired as exc:
            raise OverleafError("git_timeout", 504) from exc
        except OSError as exc:
            raise OverleafError("git_unavailable", 503) from exc
        if check and result.returncode:
            # Raw Git output can include provider details and must remain private.
            reason = result.stderr.lower()
            if b"authentication failed" in reason or b"401" in reason:
                raise OverleafError("git_authentication_failed", 401)
            if b"403" in reason or b"repository not found" in reason or b"404" in reason:
                raise OverleafError("git_access_denied", 403)
            raise OverleafError("git_request_failed", 502)
        return result

    def fetch(self, project_id):
        if not re.fullmatch(r"[0-9a-f]{24}", project_id):
            raise OverleafError("invalid_project_id")
        try:
            if not 0 < self.token_file.stat().st_size <= 4096:
                raise OSError()
        except OSError as exc:
            raise OverleafError("git_credential_missing", 503) from exc
        repo = self.root / project_id
        repo.mkdir(exist_ok=True, mode=0o700)
        if not (repo / ".git").exists():
            self.run(repo, "init", "--quiet")
        # Also repairs a process exit between init and remote configuration.
        self.run(repo, "config", "remote.origin.url", self.remote(project_id))
        heads = self.run(repo, "ls-remote", "--symref", "origin", "HEAD").stdout.decode()
        match = re.search(r"^ref: refs/heads/(main|master)\tHEAD$", heads, re.M)
        if not match:
            raise OverleafError("git_default_branch_unavailable", 502)
        branch = match[1]
        self.run(repo, "fetch", "--quiet", "--no-tags", "origin", f"refs/heads/{branch}")
        revision = self.run(repo, "rev-parse", "FETCH_HEAD^{commit}").stdout.decode().strip()
        return repo, branch, revision

    def files(self, repo, revision):
        records = self.run(repo, "ls-tree", "-rlz", revision).stdout.split(b"\0")
        entries, total = [], 0
        for record in filter(None, records):
            meta, raw_name = record.split(b"\t", 1)
            mode, kind, oid, size = meta.split()
            if mode not in (b"100644", b"100755") or kind != b"blob":
                raise OverleafError("unsupported_git_entry")
            try:
                name = source_path(raw_name.decode("utf-8"))
            except UnicodeError as exc:
                raise OverleafError("unsupported_git_path") from exc
            total += int(size)
            entries.append((name, oid, int(size)))
        if total > MAX_EXPANDED or len(entries) > MAX_FILES:
            raise OverleafError("project_limits_exceeded")
        output = io.BytesIO(self.run(repo, "cat-file", "--batch", data=b"".join(oid + b"\n" for _, oid, _ in entries)).stdout)
        files = {}
        for name, oid, size in entries:
            if output.readline().split() != [oid, b"blob", str(size).encode()]:
                raise OverleafError("git_response_invalid", 502)
            files[name] = output.read(size)
            if len(files[name]) != size or output.read(1) != b"\n":
                raise OverleafError("git_response_invalid", 502)
        return files

    def status(self, project_id, base_revision):
        with self.lock(project_id):
            repo, branch, revision = self.fetch(project_id)
            available = self.run(repo, "cat-file", "-e", base_revision + "^{commit}", check=False).returncode == 0
            result = {"project_id": project_id, "revision": revision, "branch": branch,
                      "changed": revision != base_revision, "base_available": available, "changes": None}
            if available:
                # No rename detection: rename is an explicit deletion and addition.
                raw = self.run(repo, "diff", "--no-renames", "--name-status", "-z", base_revision, revision).stdout.split(b"\0")
                result["changes"] = [{"change": raw[i].decode(), "path": raw[i+1].decode()}
                                     for i in range(0, len(raw)-1, 2)]
            return result

    def download(self, project_id):
        with self.lock(project_id):
            repo, branch, revision = self.fetch(project_id)
            files = self.files(repo, revision)
            metadata = {"project_id": project_id, "revision": revision, "branch": branch, "files": manifest(files)}
            # The envelope keeps synchronization metadata outside project sources.
            data = pack({"manifest.json": json.dumps(metadata, ensure_ascii=False, sort_keys=True).encode(),
                         **{"project/" + name: value for name, value in files.items()}})
            return {"project_id": project_id, "revision": revision, "archive_size": len(data), "archive_sha256": digest(data)}, data

    def upload(self, args, data):
        files = unpack(data)
        deleted = [source_path(name) for name in args["deleted_paths"]]
        if set(files) & set(deleted) or len(deleted) != len(set(deleted)):
            raise OverleafError("ambiguous_changes")
        project_id, base = args["project_id"], args["base_revision"]
        key, fp = "upload:" + args["idempotency_key"], fingerprint(args)
        # Same idempotency key across different projects must also be serialized.
        with self.lock(key), self.lock(project_id):
            saved = self.journal.get(key, fp)
            if saved and saved["state"] == "done":
                return saved["result"]
            repo, branch, remote = self.fetch(project_id)
            commit = saved.get("commit") if saved else None
            if commit and self.run(repo, "merge-base", "--is-ancestor", commit, remote, check=False).returncode == 0:
                result = {"status": "uploaded", "project_id": project_id, "revision": commit}
            elif remote != base:
                result = {"status": "conflict", "project_id": project_id, "revision": remote, "base_revision": base}
            else:
                if not commit:
                    existing = self.files(repo, base)  # Reject symlinks/submodules.
                    if any(name not in existing for name in deleted):
                        raise OverleafError("deleted_path_not_found")
                    combined = {**{n: v for n, v in existing.items() if n not in deleted}, **files}
                    if len(combined) > MAX_FILES or sum(map(len, combined.values())) > MAX_EXPANDED:
                        raise OverleafError("project_limits_exceeded")
                    # Validate file/directory replacements as a complete tree.
                    for name in combined:
                        if any("/".join(name.split("/")[:i]) in combined for i in range(1, len(name.split("/")))):
                            raise OverleafError("ambiguous_changes")
                    # Populate the index directly: source .gitignore and
                    # .gitattributes must not omit or transform supplied bytes.
                    modes = {r.split(b"\t", 1)[1].decode(): r.split(b" ", 1)[0]
                             for r in self.run(repo, "ls-tree", "-rz", base).stdout.split(b"\0") if r}
                    self.run(repo, "read-tree", base)
                    updates = [b"0 " + b"0"*40 + b"\t" + name.encode() + b"\0" for name in deleted]
                    for name, value in sorted(files.items()):
                        oid = self.run(repo, "hash-object", "-w", "--stdin", data=value).stdout.strip()
                        updates.append(modes.get(name, b"100644") + b" " + oid + b"\t" + name.encode() + b"\0")
                    self.run(repo, "update-index", "-z", "--index-info", data=b"".join(updates))
                    tree = self.run(repo, "write-tree").stdout.decode().strip()
                    if tree == self.run(repo, "rev-parse", base + "^{tree}").stdout.decode().strip():
                        result = {"status": "unchanged", "project_id": project_id, "revision": base}
                        self.journal.put(key, fp, {"state": "done", "result": result})
                        return result
                    commit = self.run(repo, "commit-tree", tree, "-p", base, "-F", "-", data=args["message"].encode()).stdout.decode().strip()
                    self.run(repo, "update-ref", "refs/loom/operations/" + digest(key.encode()), commit)
                    self.journal.put(key, fp, {"state": "prepared", "commit": commit})
                try:
                    self.run(repo, "push", "--porcelain", "origin", f"{commit}:refs/heads/{branch}")
                except OverleafError:
                    try:
                        _, _, current = self.fetch(project_id)
                    except OverleafError:
                        return {"status": "error", "error_code": "upload_outcome_unknown", "project_id": project_id, "revision": commit}
                    if self.run(repo, "merge-base", "--is-ancestor", commit, current, check=False).returncode == 0:
                        result = {"status": "uploaded", "project_id": project_id, "revision": commit}
                    elif current != base:
                        result = {"status": "conflict", "project_id": project_id, "revision": current, "base_revision": base}
                    else:
                        return {"status": "error", "error_code": "git_push_failed", "project_id": project_id, "revision": commit}
                else:
                    result = {"status": "uploaded", "project_id": project_id, "revision": commit}
            self.journal.put(key, fp, {"state": "done", "result": result})
            return result
