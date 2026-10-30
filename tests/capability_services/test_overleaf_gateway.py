"""Real Git repositories and controlled upstream HTTP, without account secrets."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import io
import json
from pathlib import Path
import stat
import subprocess
import threading
import time
import zipfile

import httpx
import pytest

from capability_services.suanli_overleaf_http.client import OverleafClient
from capability_services.suanli_overleaf_http.common import Journal, OverleafError, digest, fingerprint, pack, unpack
from capability_services.suanli_overleaf_http.git_store import GitStore
from capability_services.suanli_overleaf_http.server import ArchiveStream, Gateway, create_app
from capability_services.suanli_overleaf_http.web import WebSession
from loom_orchestration.grip import Names, Request
from loom_orchestration.streams import STREAM_PROTOCOL, connect_stream
from tests.support.asgi_server import running_asgi

PROJECT = "0123456789abcdef01234567"
IMPORTED = "abcdef0123456789abcdef01"


def git(path, *args):
    return subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True).stdout.decode().strip()


class LocalGit(GitStore):
    """Only the fixture enables file transport; all Git operations are real."""
    def remote(self, project_id):
        return str(self.fixture_remote)

    def run(self, repo, *args, **kwargs):
        return super().run(repo, "-c", "protocol.file.allow=always", *args, **kwargs)


@pytest.fixture
def repository(tmp_path):
    source = tmp_path / "author"
    remote = tmp_path / "remote.git"
    source.mkdir()
    git(source, "init", "-b", "main")
    git(source, "config", "user.email", "test@example.invalid")
    git(source, "config", "user.name", "Test")
    (source / "main.tex").write_text("Original 中文\n")
    (source / "delete.tex").write_text("Delete me\n")
    (source / "figure.pdf").write_bytes(bytes(range(256))*600)
    git(source, "add", ".")
    git(source, "commit", "-m", "initial")
    git(tmp_path, "clone", "--bare", str(source), str(remote))
    git(source, "remote", "add", "origin", str(remote))
    token = tmp_path / "token"
    token.write_text("fixture-token")
    store = LocalGit(tmp_path / "cache", token, Journal(tmp_path / "state"))
    store.fixture_remote = remote
    return store, source, remote


def upload_args(base, data, key="update-1", **kwargs):
    return {"project_id": PROJECT, "base_revision": base, "deleted_paths": [], "message": "Edit paper",
            "idempotency_key": key, "archive_size": len(data), "archive_sha256": digest(data), **kwargs}


def import_args(data, key="import-1"):
    return {"name": "New paper", "idempotency_key": key, "archive_size": len(data), "archive_sha256": digest(data)}


def provider(request):
    if request.headers.get("cookie") != "overleaf.sid=verified-session":
        return httpx.Response(401)
    if request.url.path == "/user/projects":
        return httpx.Response(200, json={"projects": [{"_id": PROJECT, "name": "A paper", "accessLevel": "owner"},
                                                      {"_id": IMPORTED, "name": "Shared paper", "accessLevel": "readOnly"}]})
    if request.url.path == "/project":
        return httpx.Response(200, text='<html><meta content="csrf-fixture" name="ol-csrfToken"></html>')
    if request.url.path == "/project/new/upload":
        assert request.headers["x-csrf-token"] == "csrf-fixture"
        assert request.headers["origin"] == "https://overleaf.cs.ac.cn"
        assert b'name="qqfile"; filename="project.zip"' in request.content
        assert b'name="name"' in request.content
        return httpx.Response(200, json={"success": True, "project_id": IMPORTED})
    raise AssertionError(request.url)


def login(web):
    sid = "x"*32
    web.auth_start({"session_id": sid, "callback_uri": "http://localhost:3000/login-callback", "expires_at": time.time()+300})
    assert web.browser({"action": "open", "session_id": sid})["location"] == "https://overleaf.cs.ac.cn/login"
    assert web.browser({"action": "callback", "session_id": sid, "query": {"overleaf.sid": ["anonymous"]}}) == {"status": "pending"}
    assert web.browser({"action": "callback", "session_id": sid, "query": {"overleaf.sid": ["verified-session"]}}) == {"status": "accepted"}


def test_git_roundtrip_explicit_deletions_binary_files_and_restart(repository):
    store, source, remote = repository
    metadata, data = store.download(PROJECT)
    envelope = unpack(data, envelope=True)
    assert envelope["project/figure.pdf"] == (source / "figure.pdf").read_bytes()
    assert json.loads(envelope["manifest.json"])["revision"] == metadata["revision"]
    assert not store.status(PROJECT, metadata["revision"])["changed"]
    changes = pack({"main.tex": "修改\r\n".encode(), "nested/added.tex": b"new\n", ".gitignore": b"*.tex\n", ".gitattributes": b"*.tex text eol=lf\n"})
    args = upload_args(metadata["revision"], changes, deleted_paths=["delete.tex"])
    result = store.upload(args, changes)
    assert result["status"] == "uploaded"
    assert git(remote, "show", "HEAD:main.tex") == "修改"
    assert git(remote, "ls-tree", "--name-only", "HEAD") == ".gitattributes\n.gitignore\nfigure.pdf\nmain.tex\nnested"
    _, downloaded = store.download(PROJECT)
    assert unpack(downloaded, envelope=True)["project/main.tex"] == "修改\r\n".encode()
    restored = LocalGit(store.root, store.token_file, Journal(store.journal.path.parent))
    restored.fixture_remote = remote
    assert restored.upload(args, changes) == result
    assert git(remote, "rev-list", "--count", "HEAD") == "2"
    with pytest.raises(OverleafError, match="idempotency_conflict"):
        restored.upload({**args, "message": "different"}, changes)


def test_stale_base_and_push_race_preserve_remote(repository, monkeypatch):
    store, source, remote = repository
    metadata, _ = store.download(PROJECT)
    changes = pack({"main.tex": b"agent edit"})
    original = store.run
    raced = False

    def run(repo, *args, **kwargs):
        nonlocal raced
        if args[0] == "push" and not raced:
            raced = True
            (source / "main.tex").write_text("concurrent web edit")
            git(source, "commit", "-am", "web edit")
            git(source, "push", "origin", "main")
        return original(repo, *args, **kwargs)

    monkeypatch.setattr(store, "run", run)
    result = store.upload(upload_args(metadata["revision"], changes), changes)
    assert result["status"] == "conflict"
    assert git(remote, "show", "HEAD:main.tex") == "concurrent web edit"
    status = store.status(PROJECT, metadata["revision"])
    assert status["changed"] and {"change": "M", "path": "main.tex"} in status["changes"]
    assert store.upload(upload_args(metadata["revision"], changes, "stale"), changes)["status"] == "conflict"


def test_lost_push_response_recovers_same_commit(repository, monkeypatch):
    store, _, remote = repository
    metadata, _ = store.download(PROJECT)
    changes = pack({"main.tex": b"updated"})
    args = upload_args(metadata["revision"], changes)
    original = store.run

    def run(repo, *args, **kwargs):
        result = original(repo, *args, **kwargs)
        if args[0] == "push":
            raise OverleafError("git_timeout", 504)
        return result

    monkeypatch.setattr(store, "run", run)
    result = store.upload(args, changes)
    assert result["status"] == "uploaded"
    assert store.upload(args, changes) == result
    assert git(remote, "rev-list", "--count", "HEAD") == "2"


def test_unknown_push_recovers_after_restart(repository, monkeypatch):
    store, _, remote = repository
    metadata, _ = store.download(PROJECT)
    changes = pack({"main.tex": b"recover after restart"})
    args = upload_args(metadata["revision"], changes)
    original = store.run
    pushed = False

    def run(repo, *args, **kwargs):
        nonlocal pushed
        if pushed and args[0] == "ls-remote":
            raise OverleafError("git_timeout", 504)
        result = original(repo, *args, **kwargs)
        if args[0] == "push":
            pushed = True
            raise OverleafError("git_timeout", 504)
        return result

    monkeypatch.setattr(store, "run", run)
    assert store.upload(args, changes)["error_code"] == "upload_outcome_unknown"
    restored = LocalGit(store.root, store.token_file, Journal(store.journal.path.parent))
    restored.fixture_remote = remote
    assert restored.upload(args, changes)["status"] == "uploaded"
    assert git(remote, "rev-list", "--count", "HEAD") == "2"


def test_git_credentials_are_not_urls_arguments_or_inherited_config(repository, monkeypatch):
    store, _, _ = repository
    production = GitStore(store.root, store.token_file, store.journal)
    assert production.remote(PROJECT) == "https://overleaf.cs.ac.cn/git/" + PROJECT
    monkeypatch.setenv("GIT_TRACE", "1")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    captured = []
    def execute(args, **kwargs):
        captured.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, stdout=b"", stderr=b"")
    monkeypatch.setattr(subprocess, "run", execute)
    production.run(store.root, "ls-remote", "https://overleaf.cs.ac.cn/git/"+PROJECT, "HEAD")
    argv, options = captured[0]
    assert "http.followRedirects=false" in argv and "credential.helper=" in argv
    assert "fixture-token" not in json.dumps([argv, options])
    assert "GIT_TRACE" not in options["env"] and "GIT_CONFIG_COUNT" not in options["env"]
    assert options["env"]["LOOM_OVERLEAF_TOKEN_FILE"] == str(store.token_file)


def test_master_unknown_base_and_symlink_rejection(repository):
    store, source, remote = repository
    git(remote, "branch", "master", "main")
    git(remote, "symbolic-ref", "HEAD", "refs/heads/master")
    assert store.status(PROJECT, "0"*40)["base_available"] is False
    assert store.download(PROJECT)[0]["revision"] == git(remote, "rev-parse", "master")
    git(remote, "symbolic-ref", "HEAD", "refs/heads/main")
    (source / "link").symlink_to("main.tex")
    git(source, "add", "link")
    git(source, "commit", "-m", "link")
    git(source, "push", "origin", "main")
    with pytest.raises(OverleafError, match="unsupported_git_entry"):
        store.download(PROJECT)


@pytest.mark.parametrize("name", ["../outside", "/absolute", ".git/config", "dir/.GIT/config", "C:/foo", "a\\b", "a//b", "a/./b"])
def test_unsafe_archives(name):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        archive.writestr(name, b"bad")
    with pytest.raises(OverleafError):
        unpack(output.getvalue())


def test_zip_symlink_duplicate_and_expansion_limit(monkeypatch):
    from capability_services.suanli_overleaf_http import common
    for mode in ("link", "duplicate", "large"):
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            info = zipfile.ZipInfo("source.tex")
            info.external_attr = (stat.S_IFLNK | 0o777) << 16 if mode == "link" else 0
            archive.writestr(info, b"x"*32)
            if mode == "duplicate":
                archive.writestr("source.tex", b"second")
        if mode == "large":
            monkeypatch.setattr(common, "MAX_EXPANDED", 16)
        with pytest.raises(OverleafError):
            unpack(output.getvalue())


def test_login_pending_persistence_empty_account_and_expiry(tmp_path):
    failure = {"status": None}
    def upstream(request):
        return httpx.Response(failure["status"]) if failure["status"] else provider(request)
    journal = Journal(tmp_path)
    web = WebSession(tmp_path, journal, transport=httpx.MockTransport(upstream))
    try:
        assert web.auth_status()["status"] == "auth_required"
        login(web)
        assert web.auth_status()["status"] == "authenticated"
        assert web.path.stat().st_mode & 0o777 == 0o600
        assert web.list_projects({"query": "Shared", "limit": 1})["projects"][0]["access_level"] == "readOnly"
        assert web.list_projects({"limit": 1})["next_offset"] == 1
        restored = WebSession(tmp_path, journal, transport=httpx.MockTransport(upstream))
        try:
            assert restored.auth_status()["status"] == "authenticated"
        finally:
            restored.close()
        failure["status"] = 401
        assert web.auth_status()["status"] == "auth_required"
        failure["status"] = 503
        assert web.auth_status()["status"] == "auth_failed"
        web.client.close()
        web.client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"projects": []})))
        assert web.auth_status()["status"] == "authenticated"
    finally:
        web.close()


def test_browser_challenge_only_retries_verified_anonymous_responses(tmp_path):
    control = {"response": httpx.Response(302, headers={"Location": "/login"})}
    web = WebSession(tmp_path, Journal(tmp_path), transport=httpx.MockTransport(lambda r: control["response"]))
    sid = "s"*32
    context = {"session_id": sid, "callback_uri": "http://localhost:3000/login-callback", "expires_at": time.time()+100}
    callback = {"action": "callback", "session_id": sid, "query": {"overleaf.sid": ["anonymous"]}}
    try:
        web.auth_start(context)
        web.browser({"action": "open", "session_id": sid})
        assert web.browser(callback) == {"status": "pending"}
        assert not web.path.exists() and web.challenge["expires_at"] == context["expires_at"]
        control["response"] = httpx.Response(302, headers={"Location": "https://elsewhere.invalid/login"})
        with pytest.raises(OverleafError, match="browser_login_verification_failed"):
            web.browser(callback)
        with pytest.raises(OverleafError, match="browser_session_not_found"):
            web.browser(callback)
        web.auth_start(context)
        web.browser({"action": "open", "session_id": sid})
        web.challenge["expires_at"] = time.time()-1
        with pytest.raises(OverleafError, match="browser_session_expired"):
            web.browser(callback)
    finally:
        web.close()


@pytest.mark.parametrize("outcome", ["success", "timeout", "server_error", "rejected", "malformed"])
def test_import_journal_retries_and_restart(tmp_path, outcome):
    calls = []
    def upstream(request):
        if request.method == "POST":
            calls.append(request)
            if outcome == "timeout":
                raise httpx.ReadTimeout("private upstream detail")
            if outcome == "server_error":
                return httpx.Response(500)
            if outcome == "rejected":
                return httpx.Response(422)
            if outcome == "malformed":
                return httpx.Response(200, json={"success": True})
        return provider(request)
    transport = httpx.MockTransport(upstream)
    journal = Journal(tmp_path)
    web = WebSession(tmp_path, journal, transport=transport)
    web.persist("verified-session")
    data = pack({"main.tex": b"hello"})
    args = import_args(data)
    try:
        result = web.import_project(args, data)
        assert result["status"] == ("imported" if outcome == "success" else "error")
        if outcome in ("timeout", "server_error", "malformed"):
            assert result["error_code"] == "import_outcome_unknown"
        assert web.import_project(args, data) == result
        with pytest.raises(OverleafError, match="idempotency_conflict"):
            web.import_project({**args, "name": "different"}, data)
        restored = WebSession(tmp_path, Journal(tmp_path), transport=transport)
        try:
            assert restored.import_project(args, data) == result
            crash_args = import_args(data, "crashed")
            journal.put("import:crashed", fingerprint(crash_args), {"state": "sending"})
            assert restored.import_project(crash_args, data)["error_code"] == "import_outcome_unknown"
        finally:
            restored.close()
        assert len(calls) == 1
    finally:
        web.close()


def test_concurrent_import_is_pending_and_never_resends(tmp_path):
    entered, finish = threading.Event(), threading.Event()
    def upstream(request):
        if request.method == "POST":
            entered.set()
            assert finish.wait(5)
        return provider(request)
    web = WebSession(tmp_path, Journal(tmp_path), transport=httpx.MockTransport(upstream))
    web.persist("verified-session")
    data = pack({"main.tex": b"hello"})
    args = import_args(data)
    try:
        with ThreadPoolExecutor() as executor:
            pending = executor.submit(web.import_project, args, data)
            try:
                assert entered.wait(3)
                assert web.import_project(args, data) == {"status": "pending"}
                with pytest.raises(OverleafError, match="idempotency_conflict"):
                    web.import_project({**args, "name": "changed"}, data)
            finally:
                finish.set()
            assert pending.result()["status"] == "imported"
    finally:
        web.close()


@pytest.fixture
def gateway(tmp_path, repository):
    gateway = Gateway(state_root=tmp_path / "gateway", credential_root=tmp_path,
                      instance_path=tmp_path / "instance.json", transport=httpx.MockTransport(provider))
    gateway.git = repository[0]
    yield gateway
    gateway.web.close()


class DirectSDK:
    """Exercise the package helper over the real gateway WebSocket transport."""
    def __init__(self, base):
        self.base = base

    async def open_stream(self, target, action, args):
        request = Request(version="grip.v1", request_id="archive-test", method="INVOKE", auth={"credential": "runtime-local"},
            target=target, action=Names().action(action), input=args, accepted_protocols=[STREAM_PROTOCOL])
        return await connect_stream(self.base.replace("http:", "ws:") + "/v1/projects/" + action, request)


async def test_real_stream_client_roundtrip_and_import(gateway, repository, tmp_path):
    login(gateway.web)
    async with running_asgi(create_app(gateway)) as base:
        client = OverleafClient(DirectSDK(base), Names().name("activations", "overleaf"),
                                {"projects."+name: name for name in ("download", "upload", "import")})
        directory, state = tmp_path / "download", tmp_path / "sync.json"
        await client.download(PROJECT, directory, state)
        assert (directory / "figure.pdf").read_bytes() == (repository[1] / "figure.pdf").read_bytes()
        (directory / "main.tex").write_text("client edit")
        (directory / "delete.tex").unlink()
        (directory / "ignored.aux").write_text("generated")
        result = await client.upload(directory, state, message="Local edit", idempotency_key="client-1")
        assert result["status"] == "uploaded"
        assert await client.upload(directory, state, message="Local edit", idempotency_key="client-1") == result
        assert "ignored.aux" not in git(repository[2], "ls-tree", "--name-only", "HEAD")
        imported = await client.import_project(directory, name="New paper", idempotency_key="new-paper")
        assert imported["project_id"] == IMPORTED
        assert await client.import_project(directory, name="New paper", idempotency_key="new-paper") == imported
        assert "verified-session" not in state.read_text()
        with pytest.raises(OverleafError, match="local_destination_exists"):
            await client.download(PROJECT, directory, state)


async def test_partial_or_corrupt_upload_never_mutates(gateway, repository):
    data = pack({"main.tex": b"unsafe"})
    base = git(repository[2], "rev-parse", "HEAD")
    args = upload_args(base, data)
    stream = ArchiveStream(gateway, "upload", args, lambda: None)
    await stream.write(data[:-1])
    await stream.aclose()
    assert stream.task is None
    bad = gateway.transfer("upload", args, b"x"*len(data))
    assert b"archive_integrity_failed" in bad
    assert git(repository[2], "rev-parse", "HEAD") == base
