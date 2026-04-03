---
status: STUB — to be completed by task P0-D10
---

# Triton Model Placement & VRAM Budget

> **⚠️ This is a placeholder.** The full placement matrix will be produced by a DESIGN agent executing task **P0-D10**.

## Model Inventory (draft estimates)

| Model | Type | Precision | Est. VRAM | Max Batch |
|-------|------|-----------|-----------|-----------|
| YOLOv8-L | detector | FP16 | ~90 MB | 8 |
| OSNet-x1.0 | Re-ID | FP16 | ~60 MB | 16 |
| ResNet-18 | color classifier | FP16 | ~30 MB | 32 |
| ResNet-18 | clothing classifier | FP16 | ~30 MB | 32 |

## GPU Classes (draft)

- Class A (Detection+Tracking): ~300 MB active VRAM at peak
- Class B (Re-ID+Attributes): ~200 MB active VRAM at peak
