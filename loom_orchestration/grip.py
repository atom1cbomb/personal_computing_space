"""GRIP v1 wire contracts shared by the standalone SDK and Observer."""
from __future__ import annotations

import ipaddress
import json
import math
import re
from datetime import datetime, timezone
from typing import Annotated, Any, Literal
from urllib.parse import quote, unquote, urlsplit

from pydantic import BaseModel, ConfigDict, Field, AfterValidator, model_validator

REQUEST_ID = re.compile(r"[A-Za-z0-9_.-]{1,128}\Z")


def uri(value: str) -> str:
    if any(c.isspace() for c in value):
        raise ValueError("invalid_grip_uri")
    parts = urlsplit(value)
    host = parts.netloc
    if (parts.scheme != "grip" or not value.startswith("grip://") or not host
        or len(host) > 253 or host != host.lower()
        or not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", s) for s in host.split("."))
        or not parts.path.startswith("/") or "?" in value or "#" in value):
        raise ValueError("invalid_grip_uri")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ValueError("invalid_grip_authority")
    if not re.fullmatch(r"(?:[A-Za-z0-9/._~!$&'()*+,;=:@-]|%[0-9A-F]{2})+", parts.path):
        raise ValueError("invalid_grip_path")
    for segment in parts.path.split("/"):
        if unquote(segment, errors="strict") in {".", ".."}:
            raise ValueError("invalid_grip_path")
        for encoded in re.findall(r"%[0-9A-F]{2}", segment):
            if re.fullmatch(r"[A-Za-z0-9._~-]", unquote(encoded)):
                raise ValueError("noncanonical_grip_path")
    return value


URI = Annotated[str, AfterValidator(uri)]
MCUE = Annotated[str, Field(max_length=256, pattern=r"^[a-z0-9][a-z0-9.+-]*/[a-z0-9][a-z0-9.+-]*$")]


def utc(value: str) -> str:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|\+00:00)", value):
        raise ValueError("utc_timestamp_required")
    datetime.fromisoformat(value.replace("Z", "+00:00"))
    return value


UTC = Annotated[str, AfterValidator(utc)]


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Auth(Model):
    scheme: Literal["bearer"] = "bearer"
    credential: str = Field(min_length=1, repr=False)
    tenant: URI | None = None


class Request(Model):
    version: Literal["grip.v1"]
    request_id: str = Field(pattern=REQUEST_ID)
    method: Literal["DESCRIBE", "INVOKE"]
    auth: Auth
    target: URI
    accepted_protocols: list[str] = Field(min_length=1)
    action: URI | None = None
    input: dict[str, Any] | None = None
    deadline: UTC | None = None

    @model_validator(mode="after")
    def check_method(self) -> "Request":
        if len(set(self.accepted_protocols)) != len(self.accepted_protocols) or not all(self.accepted_protocols):
            raise ValueError("invalid_accepted_protocols")
        if self.method == "DESCRIBE":
            if {"action", "input"} & self.model_fields_set:
                raise ValueError("describe_has_invoke_fields")
        elif self.action is None or self.input is None:
            raise ValueError("invoke_fields_required")
        return self


class Reply(Model):
    version: Literal["grip.v1"]
    request_id: str = Field(pattern=REQUEST_ID)
    kind: Literal["reply", "error"]
    body: dict[str, Any] | None = None
    error_code: str | None = Field(default=None, min_length=1)
    error_message: str | None = None

    @model_validator(mode="after")
    def check_kind(self) -> "Reply":
        if self.kind == "reply":
            if self.body is None or {"error_code", "error_message"} & self.model_fields_set:
                raise ValueError("invalid_reply")
        elif self.error_code is None or "body" in self.model_fields_set:
            raise ValueError("invalid_error")
        return self


class Resource(Model):
    uri: URI
    mcue: MCUE
    metadata: dict[str, Any] = Field(default_factory=dict)


class Action(Model):
    uri: URI
    mode: Literal["request-reply", "byte-stream"]
    input_schema: URI
    output_schema: URI
    description: str | None = None


class Binding(Model):
    protocol: str = Field(min_length=1)
    endpoint: str | None = None
    audience: URI | None = None


class Offer(Model):
    action: Action
    bindings: list[Binding] = Field(min_length=1)


class Description(Model):
    resource: Resource
    offers: list[Offer]
    revision: str | None = Field(default=None, min_length=1)
    expires_at: UTC | None = None

    @model_validator(mode="after")
    def check_offers(self) -> "Description":
        actions = [offer.action.uri for offer in self.offers]
        if len(actions) != len(set(actions)):
            raise ValueError("duplicate_action")
        return self

    def check_target(self, target: str) -> None:
        if self.resource.uri != target:
            raise ValueError("description_target_mismatch")
        if self.expires_at and datetime.fromisoformat(self.expires_at.replace("Z", "+00:00")) <= datetime.now(timezone.utc):
            raise ValueError("description_expired")


def decode(data: bytes) -> Any:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate_json_key")
            result[key] = value
        return result

    def invalid_constant(_):
        raise ValueError("invalid_json_number")

    def finite(value):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("invalid_json_number")
        return number

    return json.loads(data.decode("utf-8"), object_pairs_hook=unique, parse_constant=invalid_constant, parse_float=finite)


class Names:
    def __init__(self, authority: str = "loom.local", workspace: str = "workspace-default"):
        self.root = uri(f"grip://{authority}/workspaces/{quote(workspace, safe='')}")

    def name(self, *parts: str) -> str:
        return uri(self.root + "/" + "/".join(quote(part, safe="") for part in parts))

    def action(self, name: str) -> str:
        return self.name("actions", name, "v1")

    def schema(self, name: str) -> str:
        return self.name("schemas", name, "v1")

    def mcue(self, label: str, version: str = "v1") -> str:
        return self.name("mcue-types", *label.split("/"), version)


class GripError(RuntimeError):
    def __init__(self, code: str, *, remote: bool = False):
        super().__init__(code)
        self.code = code
        self.remote = remote
