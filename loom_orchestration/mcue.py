"""Versioned public resource attributes, independent of executable io.v1."""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from jsonschema import Draft202012Validator, SchemaError, ValidationError
from pydantic import Field, model_validator
from referencing import Registry, Resource
from referencing.exceptions import Unresolvable
from referencing.jsonschema import DRAFT202012

from .grip import MCUE, Model


def _schema_nodes(schema):
    if not isinstance(schema, dict):
        return
    yield schema
    for key in ("$defs", "definitions", "properties", "patternProperties", "dependentSchemas"):
        for child in schema.get(key, {}).values():
            yield from _schema_nodes(child)
    for key in ("items", "contains", "additionalProperties", "propertyNames", "not",
                "if", "then", "else", "unevaluatedItems", "unevaluatedProperties",
                "additionalItems", "contentSchema"):
        yield from _schema_nodes(schema.get(key))
    for key in ("allOf", "anyOf", "oneOf", "prefixItems"):
        for child in schema.get(key, []):
            yield from _schema_nodes(child)


def attribute_validator(schema: dict[str, Any]) -> Draft202012Validator:
    """Require a self-contained object schema; never retrieve a remote schema."""
    try:
        Draft202012Validator.check_schema(schema)
        if schema.get("type") != "object":
            raise ValueError("mcue_object_schema_required")
        if schema.get("$schema", "https://json-schema.org/draft/2020-12/schema") != "https://json-schema.org/draft/2020-12/schema":
            raise ValueError("mcue_schema_dialect_unsupported")
        base = "urn:loom:mcue:attributes"
        registry = Registry().with_resource(
            base, Resource.from_contents(schema, default_specification=DRAFT202012),
        ).crawl()
        resolver = registry.resolver(base)
        for node in _schema_nodes(schema):
            if "$id" in node:
                raise ValueError("mcue_schema_must_be_self_contained")
            for key in ("$ref", "$dynamicRef"):
                if key in node:
                    if not node[key].startswith("#"):
                        raise ValueError("mcue_schema_must_be_self_contained")
                    resolver.lookup(node[key])
        return Draft202012Validator(schema, registry=registry)
    except (SchemaError, Unresolvable) as exc:
        raise ValueError("mcue_schema_invalid") from exc


class McueDefinition(Model):
    mcue: MCUE
    version: str = Field(max_length=64, pattern=r"^v[1-9][0-9]*$")
    description: str = ""
    attributes_schema: dict[str, Any]

    @model_validator(mode="after")
    def check_schema(self):
        attribute_validator(self.attributes_schema)
        return self

    def validate_attributes(self, attributes: dict[str, Any]) -> None:
        try:
            attribute_validator(self.attributes_schema).validate(attributes)
        except (ValidationError, Unresolvable, RecursionError) as exc:
            raise ValueError("mcue_attributes_invalid") from exc


def merge_attributes(current: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(current)
    for key, value in patch.items():
        if value is None:
            result.pop(key, None)
        elif isinstance(value, dict):
            result[key] = merge_attributes(result.get(key) if isinstance(result.get(key), dict) else {}, value)
        else:
            result[key] = deepcopy(value)
    return result
