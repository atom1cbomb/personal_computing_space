from __future__ import annotations

import base64
import binascii
import io
import os
import time
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, Field
from ultralytics import YOLO

MODEL_PATH = os.getenv("YOLO_MODEL_PATH", "/models/yolo11n.pt")
MODEL_NAME = os.getenv("YOLO_MODEL_NAME", "yolo11n")
DEVICE = os.getenv("YOLO_DEVICE", "cpu")
CONFIDENCE = float(os.getenv("YOLO_CONFIDENCE", "0.25"))
MAX_IMAGE_BYTES = int(os.getenv("YOLO_MAX_IMAGE_BYTES", str(10 * 1024 * 1024)))


class DetectionRequest(BaseModel):
    request_id: str = Field(min_length=1, max_length=128)
    image_id: str = Field(min_length=1, max_length=256)
    image_base64: str = Field(min_length=1)


class Detection(BaseModel):
    class_id: int
    class_name: str
    confidence: float
    bbox: list[float]


class DetectionResponse(BaseModel):
    request_id: str
    image_id: str
    model: str
    detections: list[Detection]
    counts: dict[str, int]
    inference_ms: float


class ModelState:
    model: YOLO | None = None
    loaded_at: float | None = None


state = ModelState()


@asynccontextmanager
async def lifespan(_: FastAPI):
    state.model = YOLO(MODEL_PATH)
    state.loaded_at = time.time()
    yield
    state.model = None


app = FastAPI(title="Loom YOLO11n HTTP capability", version="1.0.0", lifespan=lifespan)


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    return {"status": "ready" if state.model is not None else "starting", "model": MODEL_NAME, "device": DEVICE, "model_path": MODEL_PATH, "loaded_at": state.loaded_at}


@app.post("/invoke", response_model=DetectionResponse)
async def invoke(payload: DetectionRequest, request: Request) -> DetectionResponse:
    if state.model is None:
        raise HTTPException(status_code=503, detail="model_not_ready")
    try:
        image_bytes = base64.b64decode(payload.image_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=400, detail="image_base64_invalid") from exc
    if len(image_bytes) > MAX_IMAGE_BYTES:
        raise HTTPException(status_code=413, detail="image_too_large")
    try:
        image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    except (UnidentifiedImageError, OSError) as exc:
        raise HTTPException(status_code=400, detail="image_invalid") from exc
    started = time.perf_counter()
    results = state.model.predict(source=image, device=DEVICE, conf=CONFIDENCE, verbose=False)
    elapsed_ms = round((time.perf_counter() - started) * 1000, 3)
    detections: list[Detection] = []
    counts: dict[str, int] = {}
    result = results[0]
    for box in result.boxes:
        class_id = int(box.cls.item())
        class_name = str(result.names[class_id])
        detections.append(Detection(class_id=class_id, class_name=class_name, confidence=round(float(box.conf.item()), 6), bbox=[round(float(value), 3) for value in box.xyxy[0].tolist()]))
        counts[class_name] = counts.get(class_name, 0) + 1
    return DetectionResponse(request_id=payload.request_id, image_id=payload.image_id, model=MODEL_NAME, detections=detections, counts=counts, inference_ms=elapsed_ms)


@app.get("/")
async def root() -> dict[str, str]:
    return {"service": "yolo11n-http", "health": "/healthz", "invoke": "/invoke"}
