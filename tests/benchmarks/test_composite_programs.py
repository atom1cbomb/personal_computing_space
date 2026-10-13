from __future__ import annotations

import pytest

from benchmarks.composite_heterogeneous_scheduling.programs import AGGREGATE_PROGRAM, RISK_PROGRAM
from loom_v2.slave.executor import ProcessJSONStdioV1Adapter


@pytest.mark.asyncio
async def test_risk_program_classifies_high_risk_with_explainable_reasons():
    result = await ProcessJSONStdioV1Adapter().invoke(
        {
            "request_id": "request-1",
            "image_id": "image-1",
            "model": "yolo11n",
            "detections": [
                {"class_name": "person", "confidence": 0.91},
                {"class_name": "person", "confidence": 0.82},
                {"class_name": "person", "confidence": 0.73},
                {"class_name": "car", "confidence": 0.66},
            ],
            "counts": {"person": 3, "car": 1},
            "inference_ms": 25.0,
        },
        program=RISK_PROGRAM.encode(),
    )

    assert result.value == {
        "request_id": "request-1",
        "image_id": "image-1",
        "counts": {"car": 1, "person": 3},
        "total_objects": 4,
        "person_count": 3,
        "vehicle_count": 1,
        "max_confidence": 0.91,
        "risk_level": "high",
        "reasons": ["person_count>=3"],
    }


@pytest.mark.asyncio
async def test_aggregate_program_reports_missing_duplicates_and_totals():
    risk_1 = {
        "request_id": "request-1",
        "image_id": "image-1",
        "counts": {"person": 3, "car": 1},
        "total_objects": 4,
        "person_count": 3,
        "vehicle_count": 1,
        "max_confidence": 0.91,
        "risk_level": "high",
        "reasons": ["person_count>=3"],
    }
    duplicate = {**risk_1, "request_id": "request-1-copy", "risk_level": "medium"}
    result = await ProcessJSONStdioV1Adapter().invoke(
        {
            "inputs": [
                {"expected_image_ids": ["image-1", "image-2"]},
                risk_1,
                duplicate,
            ]
        },
        program=AGGREGATE_PROGRAM.encode(),
    )

    assert result.value == {
        "expected_count": 2,
        "processed_count": 2,
        "unique_count": 1,
        "missing_image_ids": ["image-2"],
        "duplicate_image_ids": ["image-1"],
        "risk_counts": {"high": 1, "low": 0, "medium": 1},
        "class_counts": {"car": 2, "person": 6},
        "high_risk_image_ids": ["image-1"],
        "image_ids_digest": "76a95f38ad4e8187f15b5276f59dec3b29ade2460fb129d5ea662c49d1e96abf",
    }
