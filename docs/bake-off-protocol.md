---
status: STUB — to be completed by task P0-E01
---

# Model Bake-Off Protocol

> **⚠️ This is a placeholder.** The full protocol will be produced by an EVAL agent executing task **P0-E01**.

## Schedule (draft)

- Detector selection: 10 working days
- Tracker configuration: 5 working days
- Attribute classifier: 3 working days

## Detector Candidates (draft)

YOLOv8-L, YOLOv9-C, RT-DETR-L

## Decision Metrics (draft)

score = 0.35 * mAP + 0.25 * throughput + 0.2 * small_object_AP + 0.2 * night_AP
