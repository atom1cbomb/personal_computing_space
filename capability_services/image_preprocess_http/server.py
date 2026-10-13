from __future__ import annotations

import base64
import binascii
import hashlib
import io

from fastapi import FastAPI, HTTPException
from PIL import Image, ImageOps, UnidentifiedImageError
from pydantic import BaseModel, Field


class PreprocessRequest(BaseModel):
    request_id: str = Field(min_length=1, max_length=128)
    image_id: str = Field(min_length=1, max_length=256)
    image_base64: str = Field(min_length=1)
    max_edge: int = Field(default=1280, ge=32, le=4096)
    jpeg_quality: int = Field(default=85, ge=40, le=95)


class PreprocessResponse(BaseModel):
    request_id: str
    image_id: str
    image_base64: str
    width: int
    height: int
    input_bytes: int
    output_bytes: int
    sha256: str


app = FastAPI(title="Loom image preprocessing capability", version="1.0.0")


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ready"}


@app.post("/invoke", response_model=PreprocessResponse)
async def invoke(payload: PreprocessRequest) -> PreprocessResponse:
    try:
        image_bytes = base64.b64decode(payload.image_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=400, detail="image_base64_invalid") from exc
    try:
        with Image.open(io.BytesIO(image_bytes)) as opened:
            image = ImageOps.exif_transpose(opened).convert("RGB")
    except (UnidentifiedImageError, OSError) as exc:
        raise HTTPException(status_code=400, detail="image_invalid") from exc
    image.thumbnail((payload.max_edge, payload.max_edge), Image.Resampling.LANCZOS)
    output = io.BytesIO()
    image.save(output, format="JPEG", quality=payload.jpeg_quality, optimize=True)
    normalized = output.getvalue()
    return PreprocessResponse(
        request_id=payload.request_id,
        image_id=payload.image_id,
        image_base64=base64.b64encode(normalized).decode("ascii"),
        width=image.width,
        height=image.height,
        input_bytes=len(image_bytes),
        output_bytes=len(normalized),
        sha256=hashlib.sha256(normalized).hexdigest(),
    )


@app.get("/")
async def root() -> dict[str, str]:
    return {"service": "image-preprocess-http", "health": "/healthz", "invoke": "/invoke"}
