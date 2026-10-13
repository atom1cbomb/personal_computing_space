RISK_PROGRAM = r'''
import json
import sys

document = json.load(sys.stdin)
counts = {str(key): int(value) for key, value in sorted(document.get("counts", {}).items())}
detections = document.get("detections", [])
person_count = counts.get("person", 0)
vehicle_classes = ("bicycle", "bus", "car", "motorcycle", "truck")
vehicle_count = sum(counts.get(name, 0) for name in vehicle_classes)
total_objects = sum(counts.values())
max_confidence = round(max((float(item.get("confidence", 0)) for item in detections), default=0.0), 6)
reasons = []
if person_count >= 3:
    reasons.append("person_count>=3")
if vehicle_count >= 5:
    reasons.append("vehicle_count>=5")
if reasons:
    risk_level = "high"
else:
    if person_count >= 1:
        reasons.append("person_detected")
    if vehicle_count >= 1:
        reasons.append("vehicle_detected")
    if total_objects >= 5 and not reasons:
        reasons.append("object_count>=5")
    risk_level = "medium" if reasons else "low"
print(json.dumps({
    "request_id": document["request_id"],
    "image_id": document["image_id"],
    "counts": counts,
    "total_objects": total_objects,
    "person_count": person_count,
    "vehicle_count": vehicle_count,
    "max_confidence": max_confidence,
    "risk_level": risk_level,
    "reasons": reasons,
}, sort_keys=True))
'''.strip()


AGGREGATE_PROGRAM = r'''
import collections
import hashlib
import json
import sys

document = json.load(sys.stdin)
inputs = document["inputs"]
expected_image_ids = [str(value) for value in inputs[0]["expected_image_ids"]]
records = inputs[1:]
observed_ids = [str(record["image_id"]) for record in records]
frequencies = collections.Counter(observed_ids)
expected_set = set(expected_image_ids)
risk_counts = {"high": 0, "low": 0, "medium": 0}
class_counts = collections.Counter()
high_risk = set()
for record in records:
    risk_counts[str(record["risk_level"])] += 1
    class_counts.update({str(key): int(value) for key, value in record.get("counts", {}).items()})
    if record["risk_level"] == "high":
        high_risk.add(str(record["image_id"]))
digest_payload = json.dumps(expected_image_ids, ensure_ascii=False, separators=(",", ":")).encode()
print(json.dumps({
    "expected_count": len(expected_image_ids),
    "processed_count": len(records),
    "unique_count": len(set(observed_ids)),
    "missing_image_ids": sorted(expected_set - set(observed_ids)),
    "duplicate_image_ids": sorted(image_id for image_id, count in frequencies.items() if count > 1),
    "risk_counts": risk_counts,
    "class_counts": dict(sorted(class_counts.items())),
    "high_risk_image_ids": sorted(high_risk),
    "image_ids_digest": hashlib.sha256(digest_payload).hexdigest(),
}, sort_keys=True))
'''.strip()
