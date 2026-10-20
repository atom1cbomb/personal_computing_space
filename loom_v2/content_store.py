"""Asynchronous, immutable, content-addressed S3/MinIO storage."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Any

from loom_orchestration.grip import Names
from loom_orchestration.naming import content_digest, content_uri, default_content_name

import boto3
from botocore.exceptions import ClientError

from loom_v2.contracts.types import ResourceRef
from loom_v2.digest import canonical_json_bytes, digest_bytes, digest_json


_DIGEST_RE = re.compile(r"^[0-9a-fA-F]{64}$")


@dataclass(frozen=True)
class ContentStat:
    size: int
    declared_digest: str | None
    media_type: str
    integrity_verified: bool


class _NotFound(Exception):
    """Internal marker used to keep S3 error mapping deliberately narrow."""


class ContentStore:
    """Single S3-backed content store used by Observer and Slaves."""

    def __init__(
        self,
        *,
        endpoint_url: str | None,
        bucket: str,
        access_key: str | None,
        secret_key: str | None,
        region: str = "us-east-1",
        prefix: str = "",
        authority: str = "loom.local",
        workspace: str = "workspace-default",
    ) -> None:
        if not endpoint_url or not bucket or not access_key or not secret_key:
            raise ValueError("s3_configuration_missing")
        self.names = Names(authority, workspace)
        self.endpoint_url = endpoint_url.rstrip("/")
        self.bucket = bucket
        self.access_key = access_key
        self.secret_key = secret_key
        self.region = region or "us-east-1"
        self.prefix = prefix.strip("/")
        self._client: Any | None = None
        self._bucket_ready = False
        self._bucket_lock = asyncio.Lock()

    @classmethod
    def from_settings(cls, settings: Any) -> "ContentStore":
        """Construct a store from a Loom ``Settings`` instance."""
        return cls(
            endpoint_url=settings.s3_endpoint_url,
            bucket=settings.s3_bucket,
            access_key=settings.s3_access_key,
            secret_key=settings.s3_secret_key,
            region=settings.s3_region,
            prefix=settings.s3_prefix,
            authority=settings.grip_authority,
            workspace=settings.workspace_id,
        )

    @staticmethod
    def digest(content: bytes) -> str:
        # Byte storage uses SHA-256; the public URI also carries a readable name.
        return digest_bytes(content)

    @staticmethod
    def _normalize_digest(value: str | None) -> str:
        raw = (value or "").strip()
        if raw.startswith("grip://"):
            raw = content_digest(raw) or ""
        elif raw.startswith("sha256:"):
            raw = raw.removeprefix("sha256:")
        if not _DIGEST_RE.fullmatch(raw):
            raise ValueError("invalid_content_digest")
        return raw.lower()

    @classmethod
    def _digest_from_ref(cls, ref: ResourceRef | str) -> str:
        if isinstance(ref, ResourceRef):
            if ref.identity_criterion not in {None, "content_digest"}:
                raise ValueError("invalid_content_digest")
            return cls._normalize_digest(ref.resource_id)
        return cls._normalize_digest(ref)

    def _key(self, digest: str) -> str:
        return f"{self.prefix}/{digest}" if self.prefix else digest

    def _sync_client(self) -> Any:
        if self._client is None:
            self._client = boto3.client(
                "s3",
                endpoint_url=self.endpoint_url,
                aws_access_key_id=self.access_key,
                aws_secret_access_key=self.secret_key,
                region_name=self.region,
            )
        return self._client

    @staticmethod
    def _error_code(exc: ClientError) -> str:
        return str(exc.response.get("Error", {}).get("Code", ""))

    @classmethod
    def _is_not_found(cls, exc: ClientError) -> bool:
        return cls._error_code(exc) in {"404", "NoSuchKey", "NoSuchBucket", "NotFound"}

    @classmethod
    def _is_precondition_failed(cls, exc: ClientError) -> bool:
        return cls._error_code(exc) in {"412", "PreconditionFailed", "ConditionalRequestConflict"}

    def _ensure_bucket_sync(self) -> None:
        if self._bucket_ready:
            return
        client = self._sync_client()
        try:
            client.head_bucket(Bucket=self.bucket)
        except ClientError as exc:
            if not self._is_not_found(exc):
                if self._error_code(exc) not in {"BucketAlreadyOwnedByYou", "BucketAlreadyExists"}:
                    raise
                self._bucket_ready = True
                return
            params: dict[str, Any] = {"Bucket": self.bucket}
            if self.region != "us-east-1":
                params["CreateBucketConfiguration"] = {"LocationConstraint": self.region}
            try:
                client.create_bucket(**params)
            except ClientError as create_exc:
                if self._error_code(create_exc) not in {"BucketAlreadyOwnedByYou", "BucketAlreadyExists"}:
                    raise
        self._bucket_ready = True

    async def _ensure_bucket(self) -> None:
        async with self._bucket_lock:
            if not self._bucket_ready:
                await asyncio.to_thread(self._ensure_bucket_sync)

    def _head_sync(self, digest: str) -> dict[str, Any]:
        self._ensure_bucket_sync()
        try:
            return self._sync_client().head_object(Bucket=self.bucket, Key=self._key(digest))
        except ClientError as exc:
            if self._is_not_found(exc):
                raise _NotFound from exc
            raise

    @staticmethod
    def _metadata_digest(metadata: dict[str, Any]) -> str | None:
        for key, value in metadata.items():
            if key.lower() == "sha256":
                return str(value).lower()
        return None

    def _validate_head(self, head: dict[str, Any], *, digest: str, size: int, media_type: str) -> None:
        declared = self._metadata_digest(head.get("Metadata") or {})
        actual_type = str(head.get("ContentType") or "application/octet-stream")
        actual_size = int(head.get("ContentLength", -1))
        if declared != digest or actual_size != size or actual_type != media_type:
            raise ValueError("content_integrity_conflict")

    async def put(self, content: bytes, *, media_type: str = "application/octet-stream", name: str | None = None) -> ResourceRef:
        if not isinstance(content, bytes):
            raise TypeError("content_must_be_bytes")
        digest = self.digest(content)
        resource_id = content_uri(digest, name or default_content_name(media_type), names=self.names)
        size = len(content)
        key = self._key(digest)
        await self._ensure_bucket()

        try:
            head = await asyncio.to_thread(self._head_sync, digest)
        except _NotFound:
            head = None
        if head is not None:
            self._validate_head(head, digest=digest, size=size, media_type=media_type)
        else:
            def create() -> None:
                self._ensure_bucket_sync()
                self._sync_client().put_object(
                    Bucket=self.bucket,
                    Key=key,
                    Body=content,
                    ContentType=media_type,
                    ContentLength=size,
                    Metadata={"sha256": digest},
                    IfNoneMatch="*",
                )

            try:
                await asyncio.to_thread(create)
            except ClientError as exc:
                if not self._is_precondition_failed(exc):
                    raise
                head = await asyncio.to_thread(self._head_sync, digest)
                self._validate_head(head, digest=digest, size=size, media_type=media_type)

        # Persist the complete name independently of deduplicated byte storage.
        await asyncio.to_thread(
            self._sync_client().put_object,
            Bucket=self.bucket, Key=self._key(self._name_key(resource_id)),
            Body=resource_id.encode("utf-8"), ContentType="text/plain",
        )
        return ResourceRef(
            resource_id=resource_id,
            identity_criterion="content_digest",
            access_binding={"media_type": media_type},
        )

    @staticmethod
    def _name_key(resource_id: str) -> str:
        return "_names/" + digest_bytes(resource_id.encode("utf-8"))

    async def _require_name(self, ref: ResourceRef | str) -> None:
        target = ref.resource_id if isinstance(ref, ResourceRef) else ref
        if not target.startswith("grip://"):
            return  # Internal blob access uses an explicit checksum.
        if not target.startswith(self.names.root + "/content/"):
            raise FileNotFoundError("content_not_found")
        try:
            bound = await asyncio.to_thread(self._get_sync, self._name_key(target))
        except _NotFound as exc:
            raise FileNotFoundError("content_not_found") from exc
        if bound != target.encode("utf-8"):
            raise FileNotFoundError("content_not_found")

    def _get_sync(self, digest: str) -> bytes:
        self._ensure_bucket_sync()
        try:
            response = self._sync_client().get_object(Bucket=self.bucket, Key=self._key(digest))
        except ClientError as exc:
            if self._is_not_found(exc):
                raise _NotFound from exc
            raise
        body = response["Body"]
        try:
            return body.read()
        finally:
            body.close()

    async def get(self, ref: ResourceRef | str, *, expected_digest: str | None = None) -> bytes:
        digest = self._digest_from_ref(ref)
        await self._require_name(ref)
        if expected_digest is None:
            expected = None
        else:
            try:
                expected = self._normalize_digest(expected_digest)
            except ValueError as exc:
                raise ValueError("content_digest_mismatch") from exc
        try:
            content = await asyncio.to_thread(self._get_sync, digest)
        except _NotFound as exc:
            raise FileNotFoundError("content_not_found") from exc
        actual = self.digest(content)
        if actual != digest or (expected is not None and actual != expected):
            raise ValueError("content_digest_mismatch")
        return content

    def _stat_sync(self, digest: str) -> ContentStat:
        head = self._head_sync(digest)
        declared = self._metadata_digest(head.get("Metadata") or {})
        media_type = str(head.get("ContentType") or "application/octet-stream")
        return ContentStat(
            size=int(head.get("ContentLength", 0)),
            declared_digest=declared,
            media_type=media_type,
            integrity_verified=declared == digest,
        )

    async def exists(self, ref: ResourceRef | str) -> bool:
        digest = self._digest_from_ref(ref)
        try:
            await self._require_name(ref)
            await asyncio.to_thread(self._head_sync, digest)
            return True
        except (_NotFound, FileNotFoundError):
            return False

    async def stat(self, ref: ResourceRef | str) -> ContentStat | None:
        digest = self._digest_from_ref(ref)
        try:
            await self._require_name(ref)
            return await asyncio.to_thread(self._stat_sync, digest)
        except (_NotFound, FileNotFoundError):
            return None


async def put_typed_content(store: ContentStore, content: Any, *, media_type: str, name: str | None = None) -> ResourceRef:
    """Validate and canonicalize uploads shared by local and remote Drivers."""

    normalized_media_type = str(media_type or "").split(";", 1)[0].strip().lower()
    if not normalized_media_type:
        raise ValueError("media_type_required")
    json_media_types = {
        "application/json",
        "application/schema+json",
        "application/vnd.loom.io-contract+json",
        "application/vnd.loom.action-descriptor+json",
    }
    parsed: Any | None = None
    if normalized_media_type in json_media_types:
        if isinstance(content, (bytes, bytearray, str)):
            try:
                parsed = json.loads(content)
            except (TypeError, json.JSONDecodeError) as exc:
                raise ValueError("invalid_json") from exc
        else:
            parsed = content
        if normalized_media_type == "application/schema+json":
            from loom_v2.contracts.io_schema import validate_schema

            validate_schema(parsed)
        elif normalized_media_type == "application/vnd.loom.io-contract+json":
            from loom_v2.contracts.types import IoContract

            try:
                IoContract.model_validate(parsed)
            except Exception as exc:
                raise ValueError("io_contract_invalid") from exc
        elif normalized_media_type == "application/vnd.loom.action-descriptor+json":
            from loom_v2.contracts.resource_effects import validate_action_descriptor

            validate_action_descriptor(parsed)
        body = canonical_json_bytes(parsed)
    elif isinstance(content, str):
        body = content.encode("utf-8")
    elif isinstance(content, (bytes, bytearray)):
        body = bytes(content)
    else:
        raise TypeError("content_must_be_string_or_bytes")
    ref = await store.put(body, media_type=normalized_media_type, **({"name": name} if name is not None else {}))
    if normalized_media_type != "application/vnd.loom.action-descriptor+json":
        return ref
    return ResourceRef(
        resource_id=ref.resource_id,
        version_or_digest=digest_json(parsed, domain="loom/action-descriptor/v1"),
        identity_criterion="descriptor_digest",
        access_binding=ref.access_binding,
    )


__all__ = ["ContentStore", "ContentStat", "put_typed_content"]
