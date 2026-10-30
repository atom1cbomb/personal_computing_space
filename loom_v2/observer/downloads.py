"""Public downloads of the client artifacts built with this Observer release."""
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse


def client_downloads(directory: Path) -> APIRouter:
    router = APIRouter(prefix="/downloads", tags=["Client downloads"])

    @router.get("/{filename}", response_class=FileResponse)
    def download(filename: str) -> FileResponse:
        allowed = {"manifest.json", "personal-computing-space.zip"}
        allowed.update(path.name for path in directory.glob("loom_orchestration-*.whl"))
        path = directory / filename
        if filename not in allowed or path.is_symlink() or not path.is_file():
            raise HTTPException(status_code=404, detail="client_download_not_found")
        return FileResponse(
            path,
            filename=None if filename == "manifest.json" else filename,
            media_type="application/json" if filename == "manifest.json" else "application/zip",
            headers={"Cache-Control": "no-cache"},
        )

    return router
