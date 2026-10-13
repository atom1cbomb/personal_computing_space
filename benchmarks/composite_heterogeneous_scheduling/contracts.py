from __future__ import annotations

from typing import Any


RISK_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["request_id", "image_id", "detections", "counts", "inference_ms"],
    "properties": {
        "request_id": {"type": "string"},
        "image_id": {"type": "string"},
        "detections": {"type": "array"},
        "counts": {"type": "object"},
        "inference_ms": {"type": "number"},
    },
}

RISK_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": [
        "request_id",
        "image_id",
        "counts",
        "total_objects",
        "person_count",
        "vehicle_count",
        "max_confidence",
        "risk_level",
        "reasons",
    ],
    "properties": {
        "request_id": {"type": "string"},
        "image_id": {"type": "string"},
        "counts": {"type": "object"},
        "total_objects": {"type": "integer", "minimum": 0},
        "person_count": {"type": "integer", "minimum": 0},
        "vehicle_count": {"type": "integer", "minimum": 0},
        "max_confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "risk_level": {"enum": ["low", "medium", "high"]},
        "reasons": {"type": "array", "items": {"type": "string"}},
    },
}

AGGREGATE_INPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["inputs"],
    "properties": {"inputs": {"type": "array", "minItems": 2}},
}

AGGREGATE_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": [
        "expected_count",
        "processed_count",
        "unique_count",
        "missing_image_ids",
        "duplicate_image_ids",
        "risk_counts",
        "class_counts",
        "high_risk_image_ids",
        "image_ids_digest",
    ],
}


def io_contract(input_schema_ref: dict[str, Any], output_schema_ref: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "io.v1",
        "input_schema_ref": input_schema_ref,
        "output_schema_ref": output_schema_ref,
        "success_semantics": None,
        "success_validator_ref": None,
    }
