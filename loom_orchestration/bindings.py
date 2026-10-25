"""Client-owned binding trust and scoped credential values."""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit

from .grip import Binding, GripError


@dataclass
class AccessCredential:
    credential: str = field(repr=False)
    expires_at: float | None = None

    @classmethod
    def from_response(cls, body):
        expires = body.get("expires_at")
        if isinstance(expires, str):
            expires = datetime.fromisoformat(expires.replace("Z", "+00:00")).timestamp()
        return cls(body["access_token"], expires)


def stream_endpoint(base: str) -> str:
    parts = urlsplit(base.rstrip("/"))
    return urlunsplit(("wss" if parts.scheme == "https" else "ws", parts.netloc, parts.path + "/grip/stream", "", ""))


def validate_endpoint(binding: Binding, *, allow_insecure: bool = False) -> None:
    parts = urlsplit(binding.endpoint or "")
    schemes = {"https-json.v1": "https", "loom-http-json.v1": "http", "loom-wss-stream.v1": "wss"}
    expected = schemes.get(binding.protocol)
    if expected is None:
        raise GripError("unsupported_binding")
    if allow_insecure and expected == "wss" and parts.scheme == "ws":
        expected = "ws"
    if parts.scheme != expected or (parts.scheme == "http" and not allow_insecure):
        raise GripError("unsupported_binding")
    if not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
        raise GripError("untrusted_endpoint")


def timestamp(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()
