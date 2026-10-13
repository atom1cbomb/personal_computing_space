from __future__ import annotations

import json
from pathlib import Path

import jsonschema


ROOT = Path(__file__).resolve().parents[2] / "capability_services" / "yolo11n_http"


def test_yolo_request_and_response_examples_match_schemas():
    input_schema = json.loads((ROOT / "schemas/detect-input.json").read_text())
    output_schema = json.loads((ROOT / "schemas/detect-output.json").read_text())

    jsonschema.validate(
        {"request_id": "request-1", "image_id": "image-1", "image_base64": "YWJj"},
        input_schema,
    )
    jsonschema.validate(
        {
            "request_id": "request-1",
            "image_id": "image-1",
            "model": "yolo11n",
            "detections": [],
            "counts": {},
            "inference_ms": 12.5,
        },
        output_schema,
    )


def test_yolo_package_uses_container_http_invoke_contract():
    package = json.loads((ROOT / "package.example.json").read_text())

    assert package["package_id"] == "yolo11n-http"
    assert package["execution"] == {"kind": "container:http", "version": "1"}
    assert package["capability_exports"][0]["runtime_binding"] == {"path": "/invoke"}
