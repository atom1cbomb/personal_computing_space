"""Typed JSON for the fixed, authenticated repository boundary."""
from dataclasses import fields, is_dataclass
from datetime import datetime

from pydantic import BaseModel
from loom_v2.contracts import types, agents
from loom_v2 import repository_state

TYPES = {name: value for module in (types, agents, repository_state)
         for name, value in vars(module).items()
         if isinstance(value, type) and (issubclass(value, BaseModel) or is_dataclass(value))}


def encode(value):
    if isinstance(value, BaseModel):
        return {"type": type(value).__name__, "value": value.model_dump(mode="json")}
    if is_dataclass(value):
        return {"type": type(value).__name__, "value": {f.name: encode(getattr(value, f.name)) for f in fields(value)}}
    if isinstance(value, datetime):
        return {"type": "datetime", "value": value.isoformat()}
    if isinstance(value, (list, tuple, set)):
        return {"type": "set" if isinstance(value, set) else "list", "value": [encode(v) for v in value]}
    if isinstance(value, dict):
        return {"type": "dict", "value": {k: encode(v) for k, v in value.items()}}
    return value


def decode(value):
    if not isinstance(value, dict):
        return value
    kind, data = value["type"], value["value"]
    if kind == "dict":
        return {k: decode(v) for k, v in data.items()}
    if kind in {"list", "set"}:
        result = [decode(v) for v in data]
        return set(result) if kind == "set" else result
    if kind == "datetime":
        return datetime.fromisoformat(data)
    cls = TYPES[kind]
    if issubclass(cls, BaseModel):
        return cls.model_validate(data)
    return cls(**{k: decode(v) for k, v in data.items()})
