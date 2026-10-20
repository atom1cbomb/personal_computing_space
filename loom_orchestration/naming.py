"""Shared resource identities; transport URLs and storage keys are separate."""
from __future__ import annotations

import re
from urllib.parse import unquote, urlsplit

from .grip import Names, uri

CONTENT_PATTERN = r"^grip://[a-z0-9.-]+/workspaces/[^/]+/content/(?:[^/]+/)+sha256/[0-9a-f]{64}$"
_CONTENT = re.compile(CONTENT_PATTERN)


def content_digest(resource_id: str) -> str | None:
    """Return the byte digest only for a canonical, named content URI."""
    if not _CONTENT.fullmatch(resource_id):
        return None
    uri(resource_id)
    return resource_id.rsplit("/", 1)[1]


def content_uri(digest: str, name: str = "document", *, names: Names | None = None) -> str:
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("invalid_content_digest")
    parts = name.split("/")
    if any(not p or p in {".", ".."} for p in parts) or ":" in name:
        raise ValueError("invalid_content_name")
    if not any(re.search(r"[a-zA-Z]", p) and not re.fullmatch(r"[0-9a-fA-F]{32,}", p) for p in parts):
        raise ValueError("readable_content_name_required")
    return (names or Names()).name("content", *parts, "sha256", digest)


def content_name(resource_id: str) -> str:
    if not content_digest(resource_id):
        raise ValueError("content_ref_required")
    path = urlsplit(resource_id).path.split("/")
    return "/".join(unquote(p) for p in path[4:-2])


def resource_label(resource_id: str) -> str:
    """Readable action label for action names, never a decoded nested URI."""
    if content_digest(resource_id):
        return content_name(resource_id).rsplit("/", 1)[-1]
    return unquote(urlsplit(uri(resource_id)).path.rsplit("/", 1)[-1])


def default_content_name(media_type: str) -> str:
    return {
        "application/json": "json-document",
        "application/schema+json": "json-schema",
        "application/vnd.loom.io-contract+json": "io-contract",
        "application/vnd.loom.action-descriptor+json": "action-descriptor",
        "text/x-python": "python-program",
        "text/plain": "text-document",
    }.get(media_type, "binary-content")
