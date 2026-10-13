from __future__ import annotations

import base64

import httpx
import pytest

from capability_services.image_preprocess_http.server import app


def _ppm(width: int = 64, height: int = 32) -> bytes:
    pixels = bytes([220, 20, 20] * width * height)
    return f"P6\n{width} {height}\n255\n".encode() + pixels


@pytest.mark.asyncio
async def test_preprocess_normalizes_and_bounds_image():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://preprocess") as client:
        response = await client.post(
            "/invoke",
            json={
                "request_id": "request-1",
                "image_id": "image-1",
                "image_base64": base64.b64encode(_ppm()).decode("ascii"),
                "max_edge": 32,
                "jpeg_quality": 85,
            },
        )

    assert response.status_code == 200
    value = response.json()
    assert value["request_id"] == "request-1"
    assert value["image_id"] == "image-1"
    assert (value["width"], value["height"]) == (32, 16)
    assert value["input_bytes"] == len(_ppm())
    assert value["output_bytes"] > 0
    assert len(value["sha256"]) == 64
    assert base64.b64decode(value["image_base64"]).startswith(b"\xff\xd8")


@pytest.mark.asyncio
async def test_preprocess_rejects_invalid_base64():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://preprocess") as client:
        response = await client.post(
            "/invoke",
            json={
                "request_id": "request-1",
                "image_id": "image-1",
                "image_base64": "not-base64!",
            },
        )

    assert response.status_code == 400
    assert response.json()["detail"] == "image_base64_invalid"
