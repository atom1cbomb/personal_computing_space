"""Schema-declared public resource dependencies; no provider-specific semantics."""
from __future__ import annotations

from typing import Any

from .grip import uri


DEPENDENCY = "x-loom-dependency"


def dependency_fields(schema: dict[str, Any]) -> list[dict[str, Any]]:
    """Read explicit properties/items paths and reject hidden dependency markers."""
    result, recognized = [], set()

    def visit(node, path, required):
        if not isinstance(node, dict):
            return
        if DEPENDENCY in node:
            recognized.add(id(node))
            many = node.get("type") == "array"
            item = node.get("items", {}) if many else node
            if (node[DEPENDENCY] is not True or not path or not isinstance(item, dict)
                    or item.get("type") != "string" or (many and DEPENDENCY in item)
                    or not isinstance(node.get("description"), str) or not node["description"].strip()):
                raise ValueError("dependency_declaration_invalid")
            result.append({"path": path, "description": node["description"],
                           "required": required, "many": many})
        for name, child in node.get("properties", {}).items():
            if "." in name or "[" in name or "]" in name:
                # Such ordinary attributes remain valid, but cannot be dependency paths.
                continue
            visit(child, f"{path}.{name}" if path else name,
                  required and name in node.get("required", []))
        if isinstance(node.get("items"), dict):
            visit(node["items"], path + "[]", required)

    visit(schema, "", True)
    # Only examine schema nodes, not examples/defaults containing business JSON.
    def check(node):
        if not isinstance(node, dict):
            return
        if DEPENDENCY in node and id(node) not in recognized:
            raise ValueError("dependency_annotation_location_unsupported")
        for key in ("$defs", "definitions", "properties", "patternProperties", "dependentSchemas"):
            for child in node.get(key, {}).values():
                check(child)
        for key in ("items", "contains", "additionalProperties", "propertyNames", "not", "if", "then", "else",
                    "unevaluatedItems", "unevaluatedProperties", "additionalItems", "contentSchema"):
            check(node.get(key))
        for key in ("allOf", "anyOf", "oneOf", "prefixItems"):
            for child in node.get(key, []):
                check(child)
    check(schema)
    return result


def path_values(value: Any, path: str) -> list[Any]:
    values = [value]
    for part in path.split("."):
        key = part.split("[", 1)[0]
        values = [v[key] for v in values if isinstance(v, dict) and key in v]
        for _ in range(part.count("[]")):
            values = [item for v in values if isinstance(v, list) for item in v]
    return values


def resource_dependencies(schema: dict[str, Any], value: dict[str, Any]) -> list[dict[str, str]] | None:
    """Project only declared public references. None means no dependency declaration."""
    fields = dependency_fields(schema)
    if not fields:
        return None
    result = []
    for field in fields:
        values = path_values(value, field["path"])
        if field["many"]:
            values = [item for v in values for item in v]
        for reference in values:
            try:
                uri(reference)
            except (TypeError, ValueError):
                raise ValueError("dependency_reference_invalid:" + field["path"]) from None
            result.append({"path": field["path"], "resource": reference, "description": field["description"]})
    return result
