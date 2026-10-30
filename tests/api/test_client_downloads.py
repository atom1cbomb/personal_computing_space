"""Release downloads are public and cannot expose other Observer files."""
import json

from fastapi.testclient import TestClient
import pytest

from loom_v2.observer.app import create_app
from loom_v2.settings import Settings


@pytest.fixture
def downloads(tmp_path):
    directory = tmp_path / "downloads"
    directory.mkdir()
    wheel = "loom_orchestration-0.12.0-py3-none-any.whl"
    (directory / wheel).write_bytes(b"test-wheel")
    (directory / "personal-computing-space.zip").write_bytes(b"test-skill")
    (directory / "manifest.json").write_text(json.dumps({"sdk": {"filename": wheel}}))
    (directory / "private.json").write_text('secret')
    return directory, wheel


def test_downloads_work_without_token_and_use_attachment_filenames(downloads):
    directory, wheel = downloads
    with TestClient(create_app(settings=Settings(client_download_dir=str(directory), grip_token="owner-secret"))) as client:
        manifest = client.get("/downloads/manifest.json")
        assert manifest.status_code == 200
        assert manifest.json()["sdk"]["filename"] == wheel
        assert manifest.headers["content-type"] == "application/json"
        for name, expected in ((wheel, b"test-wheel"), ("personal-computing-space.zip", b"test-skill")):
            response = client.get("/downloads/" + name)
            assert response.status_code == 200
            assert response.content == expected
            assert response.headers["content-disposition"] == f'attachment; filename="{name}"'
            assert response.headers["cache-control"] == "no-cache"
        partial = client.get("/downloads/" + wheel, headers={"Range": "bytes=0-3"})
        assert partial.status_code == 206 and partial.content == b"test"
        # A fixed skill URL must return the new release after its files change.
        (directory / "personal-computing-space.zip").write_bytes(b"new-release-skill")
        response = client.get("/downloads/personal-computing-space.zip")
        assert response.content == b"new-release-skill"
        assert response.headers["cache-control"] == "no-cache"


@pytest.mark.parametrize("name", ["private.json", "missing.zip", "../private.json", "%2e%2e%2fprivate.json"])
def test_downloads_do_not_expose_unpublished_files(downloads, name):
    directory, _ = downloads
    with TestClient(create_app(settings=Settings(client_download_dir=str(directory)))) as client:
        assert client.get("/downloads/" + name).status_code == 404


def test_unbuilt_downloads_return_404_without_breaking_observer(tmp_path):
    with TestClient(create_app(settings=Settings(client_download_dir=str(tmp_path / "missing")))) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/downloads/manifest.json").status_code == 404
        assert client.get("/downloads/personal-computing-space.zip").status_code == 404


def test_downloads_reject_symlinks(downloads):
    directory, wheel = downloads
    (directory / wheel).unlink()
    (directory / wheel).symlink_to(directory / "private.json")
    with TestClient(create_app(settings=Settings(client_download_dir=str(directory)))) as client:
        assert client.get("/downloads/" + wheel).status_code == 404
