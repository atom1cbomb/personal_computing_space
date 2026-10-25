"""GRIP business actions and the AutoPaper HTTP adapter share one upstream."""
from __future__ import annotations

import asyncio
import json
import math
import os
from pathlib import Path

import httpcore
import httpx
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from loom_orchestration.dependencies import DependencyContext
from loom_orchestration.grip import GripError, URI
from loom_orchestration.stream_http import StreamHTTPTransport


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_resource_ref: dict | None = None
    service_ref: URI
    dependency: str = "kubernetes"
    request_timeout_seconds: float = Field(default=120, gt=0, le=3600)


class EmbedInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    texts: list[str] = Field(min_length=1, max_length=256)
    normalize: bool = True
    instruction: str | None = None


class EmbedOneInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str
    normalize: bool = True
    instruction: str | None = None


class EmbedOutput(BaseModel):
    embeddings: list[list[float]]
    dimension: int = Field(gt=0)


class InfoOutput(BaseModel):
    model: str = Field(min_length=1)
    dimension: int = Field(gt=0)
    device: str


class HealthOutput(BaseModel):
    status: str
    model_loaded: bool


class EmbeddingService:
    def __init__(self, config, *, context=None, opener=None):
        self.config = Config.model_validate(config)
        self.context = context or DependencyContext()
        self.transport = StreamHTTPTransport(opener or self.open_upstream)
        self.client = httpx.AsyncClient(transport=self.transport, base_url="http://model.internal",
            timeout=self.config.request_timeout_seconds, follow_redirects=False, trust_env=False)

    async def open_upstream(self):
        async with self.context.client(self.config.dependency, min_valid_seconds=self.config.request_timeout_seconds) as (client, dependency):
            offers = [o for o in dependency["description"]["offers"] if o["action"]["mode"] == "byte-stream"]
            if len(offers) != 1:
                raise GripError("unsupported_action")
            return await client.open_stream(dependency["target"], offers[0]["action"]["uri"],
                {"service_ref": self.config.service_ref})

    async def request(self, method, path, payload=None):
        # No HTTP retries. Every request owns a fresh GRIP channel.
        try:
            async with asyncio.timeout(self.config.request_timeout_seconds):
                async with self.client.stream(method, path, json=payload if method == "POST" else None) as response:
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > 32 * 1024 * 1024:
                            raise HTTPException(502, "embedding_response_too_large")
                    if response.status_code >= 400:
                        # Forward only structured validation errors; upstream
                        # exceptions and private connection details stay internal.
                        if response.status_code == 422:
                            raise HTTPException(422, "embedding_input_invalid")
                        raise HTTPException(502, "embedding_upstream_failed")
                    return json.loads(body)
        except (TimeoutError, httpcore.TimeoutException, httpx.TimeoutException) as exc:
            raise HTTPException(504, "embedding_timeout") from exc
        except (GripError, httpx.HTTPError, httpcore.NetworkError, httpcore.ProtocolError, OSError) as exc:
            raise HTTPException(503, "embedding_unavailable") from exc
        except ValueError as exc:
            raise HTTPException(502, "embedding_output_invalid") from exc

    async def info(self):
        try:
            return InfoOutput.model_validate(await self.request("GET", "/info")).model_dump()
        except ValidationError as exc:
            raise HTTPException(502, "embedding_output_invalid") from exc

    async def health(self):
        try:
            value = HealthOutput.model_validate(await self.request("GET", "/health"))
            if value.status != "ok" or not value.model_loaded:
                raise HTTPException(503, "model_not_ready")
            return value.model_dump()
        except ValidationError as exc:
            raise HTTPException(502, "embedding_output_invalid") from exc

    async def embed(self, payload):
        try:
            value = EmbedOutput.model_validate(await self.request("POST", "/embed", payload.model_dump()))
            if len(value.embeddings) != len(payload.texts) or any(len(v) != value.dimension or not all(math.isfinite(x) for x in v) for v in value.embeddings):
                raise HTTPException(502, "embedding_output_invalid")
            return value.model_dump()
        except ValidationError as exc:
            raise HTTPException(502, "embedding_output_invalid") from exc

    async def close(self):
        await self.client.aclose()


def create_apps(service):
    private = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    consumer = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @private.get("/healthz")
    async def liveness():
        return {"status": "ok"}

    @private.post("/v1/model/info")
    async def info_action():
        return await service.info()

    @private.post("/v1/model/embed")
    async def embed_action(payload: EmbedInput):
        return await service.embed(payload)

    @consumer.get("/health")
    async def health():
        return await service.health()

    @consumer.get("/info")
    async def info():
        return await service.info()

    @consumer.post("/embed")
    async def embed(payload: EmbedInput):
        return await service.embed(payload)

    @consumer.post("/embed-one")
    async def embed_one(payload: EmbedOneInput):
        result = await service.embed(EmbedInput(texts=[payload.text], normalize=payload.normalize, instruction=payload.instruction))
        return {"embedding": result["embeddings"][0], "dimension": result["dimension"]}
    return private, consumer


async def main():
    import uvicorn
    config = json.loads(Path(os.getenv("LOOM_INSTANCE_CONFIG", "/run/loom/instance.json")).read_text())
    service = EmbeddingService(config)
    private, consumer = create_apps(service)
    # Docker exposes neither port by default. The operator can publish only
    # consumer 8081 onto host loopback; the private runtime API stays separate.
    servers = [uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=port, access_log=False))
               for app, port in ((private, 8080), (consumer, 8081))]
    tasks = [asyncio.create_task(server.serve()) for server in servers]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for server in servers:
            server.should_exit = True
        await asyncio.gather(*tasks, return_exceptions=True)
        await service.close()


if __name__ == "__main__":
    asyncio.run(main())
