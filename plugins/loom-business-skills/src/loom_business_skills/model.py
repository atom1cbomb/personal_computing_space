from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Annotated
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator
from loom_orchestration.grip import URI

Slug = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]{0,63}$")]
Short = Annotated[str, Field(min_length=1, max_length=240)]
Instruction = Annotated[str, Field(min_length=1, max_length=600)]


class Problem(Exception):
    def __init__(self, code, **details):
        self.code, self.details = code, details
        super().__init__(code)


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Binding(Model):
    target: URI
    action: URI
    dependencies: list[URI] = Field(default_factory=list, max_length=16)


class Step(Model):
    id: Slug
    binding: Slug
    instruction: Instruction
    acceptance: Instruction
    on_failure: Instruction


class Draft(Model):
    id: Slug
    title: Annotated[str, Field(min_length=1, max_length=80)]
    summary: Short
    keywords: list[Annotated[str, Field(min_length=1, max_length=40)]] = Field(min_length=1, max_length=12)
    bindings: dict[Slug, Binding] = Field(min_length=1, max_length=8)
    steps: list[Step] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def coherent(self):
        if len({s.id for s in self.steps}) != len(self.steps):
            raise ValueError("duplicate_step")
        if {s.binding for s in self.steps} != set(self.bindings):
            raise ValueError("step_binding_mismatch")
        if sum(len(s.instruction) + len(s.acceptance) + len(s.on_failure) for s in self.steps) > 5000:
            raise ValueError("experience_context_budget_exceeded")
        return self


def now():
    return datetime.now(timezone.utc).isoformat()


def encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def fingerprint(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def environment(client):
    u = urlsplit(client.base_url)
    if u.scheme not in {"https", "http"} or not u.hostname or u.username or u.password or u.query or u.fragment:
        raise Problem("invalid_observer_url")
    return {"observer": client.base_url.rstrip("/"), "workspace": client.names.root}


def reject_secrets(value, *, token=None):
    """Catch common accidental credential pastes, not a general data-loss scanner."""
    if isinstance(value, dict):
        for item in value.values():
            reject_secrets(item, token=token)
    elif isinstance(value, list):
        for item in value:
            reject_secrets(item, token=token)
    elif isinstance(value, str):
        patterns = [r"-----BEGIN [A-Z ]*PRIVATE KEY-----", r"\bBearer\s+[A-Za-z0-9._~+/=-]{12,}",
                    r"\b(?:api[_-]?key|access[_-]?token|password|cookie)\s*[=:]\s*['\"]?[A-Za-z0-9._~+/=-]{12,}",
                    r"\b(?:sk-|ghp_|github_pat_)[A-Za-z0-9_-]{20,}"]
        if (token and len(token) >= 8 and token in value) or any(re.search(p, value, re.I) for p in patterns):
            raise Problem("credential_in_experience")


def brief(record, *, historical=False):
    d = record["draft"]
    return {"id": d["id"], "title": d["title"], "summary": d["summary"],
            "historical_only": historical,
            "steps": [{**s, "verification": record["evidence"].get(s["id"], {}).get("level", "documented")}
                      for s in d["steps"]]}


def markdown(record):
    d = record["draft"]
    lines = ["---", "name: " + d["id"], "description: " + json.dumps(d["summary"], ensure_ascii=False), "---", "",
             "# " + d["title"], "", "Historical experience, not current authority. Use the plugin's prepare command",
             "before each task; explicit rebind is required for another Observer/workspace.",
             "Do not install this cached file into an agent's automatic skill discovery directories.", ""]
    for s in d["steps"]:
        level = record["evidence"].get(s["id"], {}).get("level", "documented")
        lines.extend([f"## {s['id']} ({level})", "", s["instruction"],
                      "Acceptance: " + s["acceptance"], "Failure: " + s["on_failure"], ""])
    return "\n".join(lines)
