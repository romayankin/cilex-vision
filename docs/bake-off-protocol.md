---
version: "1.0.0"
status: P0-E01
created_by: eval-agent
date: "2026-04-06"
---

# Model Bake-Off Protocol

This document defines the formal selection protocol for the detector, tracker,
and attribute models used in the Cilex Vision pilot. It replaces the earlier
stub and is the normative reference for the evaluation harnesses in
`scripts/bakeoff/`.

The protocol is intentionally constrained by the rest of the architecture:

- the detector must recognize exactly the 7 classes from `docs/taxonomy.md`
- detector candidates must fit the Triton deployment shape in
  `docs/triton-placement.md`: FP16 TensorRT, 640x640 RGB input, batch <= 8,
  max queue delay 50 ms, single 24 GB GPU
- evaluation runs must be logged to MLflow with parameters, metrics, and
  artifacts
- if the time box expires without a clear winner, choose the safest default

## 1. Global Rules

### 1.1 Evaluation Logging

Every evaluation run MUST log to MLflow:

- candidate name and artifact paths
- git SHA or working tree revision, when available
- Triton / TensorRT settings
- dataset manifest path and split identifiers
- all reported metrics
- artifacts:
  - JSON summary
  - Markdown comparison input table
  - charts produced by `compare_bakeoff.py`

### 1.2 Dataset Requirements

The evaluation dataset is expected under `data/eval/`. Because this repository
does not yet contain annotated evaluation data, the scripts must fail fast with
a clear message when manifests or image files are missing.

Detector evaluation data MUST provide:

- the 7 taxonomy classes:
  - `person`
  - `car`
  - `truck`
  - `bus`
  - `bicycle`
  - `motorcycle`
  - `animal`
- bounding boxes in COCO-style `xywh` format at original image resolution
- an image-level night flag:
  - `is_night: true|false`
- enough small-object examples to compute a stable small-object AP slice

Tracker evaluation data MUST provide:

- per-frame detections or detector outputs
- ground-truth track IDs
- camera-consistent timestamps

Attribute evaluation data MUST provide:

- object crops
- the 10-color target vocabulary from `docs/taxonomy.md`
- per-crop labels for the relevant attribute family

### 1.3 Operational Gates

The following gates apply to the final recommendation:

- detector winner MUST support FP16 TensorRT export and Triton deployment
- detector winner MUST preserve the 7-class taxonomy exactly
- detector winner SHOULD sustain at least 40 FPS aggregate on the pilot rig
  because the pilot target is 4 cameras x 10 FPS
- no candidate may be selected if it requires a different trust boundary,
  message format, or deployment architecture than the rest of the repo

## 2. Detector Bake-Off

### 2.1 Time Box

- duration: 10 working days
- owner: EVAL
- output:
  - MLflow runs per candidate
  - Markdown comparison
  - charts
  - recommendation

### 2.2 Candidates

The detector bake-off compares these three candidates:

| Candidate | Family | Deployment Constraint |
|-----------|--------|-----------------------|
| YOLOv8-L | anchor-free one-stage detector | Must run at 640x640, FP16, Triton batch <= 8 |
| YOLOv9-C | one-stage detector | Must run at 640x640, FP16, Triton batch <= 8 |
| RT-DETR-L | transformer detector | Must still fit the same Triton envelope; no exception for latency |

### 2.3 Input / Runtime Contract

All detector candidates MUST be evaluated under the same deployment envelope:

- ONNX export
- TensorRT FP16 engine build
- Triton Inference Server in EXPLICIT mode
- RGB input, normalized to `[0.0, 1.0]`
- letterboxed resize to `640x640`
- max batch size `8`
- preferred batches `1, 4, 8`
- max queue delay `50 ms`

This keeps the bake-off aligned with `docs/triton-placement.md` and avoids
selecting a detector that wins offline but cannot be deployed in the intended
runtime.

### 2.4 Metrics

The detector report MUST include:

- `mAP@0.5`
- `mAP@0.5:0.95`
- per-class AP for all 7 classes
- latency:
  - p50
  - p95
  - p99
- throughput in FPS
- `small_object_AP`
- `night_AP`

Metric definitions:

- `mAP@0.5`: mean AP at IoU 0.50
- `mAP@0.5:0.95`: COCO-style mean AP averaged over IoU thresholds
  `0.50, 0.55, ..., 0.95`
- per-class AP: class-specific `AP@0.5:0.95`
- `small_object_AP`: `AP@0.5:0.95` on GT boxes with area `< 32^2` pixels in
  the original frame
- `night_AP`: `AP@0.5:0.95` on images flagged `is_night = true`
- throughput: measured on Triton after TensorRT engine load, not from offline
  PyTorch or ONNXRuntime inference

### 2.5 Confidence Threshold Rule

The detector report MUST preserve the pilot taxonomy threshold policy:

- operational threshold during the pilot is `0.40` for all 7 classes
- ranking metrics remain AP-based and therefore threshold-independent
- the report MUST also include an operational slice at `0.40` confidence so
  the chosen detector is not selected on AP alone while behaving poorly at the
  pilot threshold

### 2.6 Decision Formula

The detector decision formula is:

```text
score =
  0.35 * mAP
  + 0.25 * throughput
  + 0.20 * small_obj
  + 0.20 * night_perf
```

Normalization rules:

- `mAP = mAP@0.5:0.95`
- `small_obj = small_object_AP`
- `night_perf = night_AP`
- `throughput = min(measured_fps / 40.0, 1.0)`

The throughput normalization anchors the score to the pilot requirement
(`4 cameras x 10 FPS = 40 FPS`) rather than to the best run in a single
comparison. This makes runs comparable across days and hardware-stable enough
for MLflow reporting.

### 2.7 Clear Winner Rule

A detector is a clear winner only if:

1. it has the highest composite `score`
2. its score exceeds the runner-up by at least `0.02` absolute
3. it does not violate the deployment contract in §2.3

If those conditions are not met by the end of day 10, choose the safest
default:

- **safest default detector: `YOLOv8-L`**

Rationale:

- it already matches the current Triton placement spec
- it is the least risky path for the pilot if the bake-off is inconclusive
- selecting it avoids reopening the deployment envelope under time pressure

## 3. Tracker Bake-Off

### 3.1 Time Box

- duration: 5 working days

### 3.2 Candidates

| Candidate | Notes |
|-----------|-------|
| ByteTrack | current architectural default |
| BoT-SORT | alternative MOT tracker with stronger appearance association |

### 3.3 Metrics

The tracker report MUST include:

- `MOTA`
- `IDF1`
- `ID_switches`
- `fragmentation`

The report SHOULD also include:

- lost-track recovery behavior
- throughput on representative camera streams
- operational notes for tracker reset / reinitialization behavior

### 3.4 Decision Rule

Primary ranking order:

1. highest `IDF1`
2. highest `MOTA`
3. lowest `ID_switches`
4. lowest `fragmentation`

If no clear winner emerges inside the 5-day time box:

- **safest default tracker: `ByteTrack`**

Rationale:

- it is already assumed in the architecture and Triton placement notes
- it is CPU-only and operationally simpler
- it minimizes integration risk for the pilot

## 4. Attribute Bake-Off

### 4.1 Time Box

- duration: 3 working days

### 4.2 Candidate Family

Two color classifiers must be compared. The baseline candidate is fixed:

| Candidate | Notes |
|-----------|-------|
| ResNet-18 color classifier | baseline already assumed by `docs/triton-placement.md` |
| EfficientNet-B0 color classifier | challenger with similar deployment complexity |

Both candidates MUST emit the same 10-color vocabulary:

`red, blue, white, black, silver, green, yellow, brown, orange, unknown`

### 4.3 Metrics

The attribute report MUST include:

- overall accuracy
- per-color accuracy
- confusion matrix

The report SHOULD also include:

- latency and throughput under the Triton batch-32 attribute envelope
- robustness notes for IR / low-light crops

### 4.4 Decision Rule

Primary ranking order:

1. highest macro per-color accuracy
2. lowest confusion among common operational confusions:
   - black vs blue
   - white vs silver
   - brown vs orange
3. lowest latency at equal accuracy, if the gap is operationally meaningful

If no clear winner emerges inside the 3-day time box:

- **safest default attribute classifier: `ResNet-18`**

Rationale:

- it is already budgeted in the Triton placement spec
- it preserves the current VRAM and batching assumptions
- changing it late in Phase 0 offers limited benefit relative to integration
  risk

## 5. Procedure by Phase

### 5.1 Detector Procedure

1. Freeze the evaluation manifest and class mapping.
2. Export every detector candidate to ONNX.
3. Build FP16 TensorRT engines with the same dynamic shape profile.
4. Install each engine into a Triton model repository.
5. Load each candidate explicitly via Triton.
6. Run inference over the evaluation set.
7. Compute:
   - `mAP@0.5`
   - `mAP@0.5:0.95`
   - per-class AP
   - latency p50/p95/p99
   - throughput FPS
   - `small_object_AP`
   - `night_AP`
8. Log the run to MLflow.
9. Compare runs with `scripts/bakeoff/compare_bakeoff.py`.
10. Apply the clear-winner rule in §2.7.

### 5.2 Tracker Procedure

1. Freeze detector input for all tracker candidates.
2. Replay identical clips through each tracker.
3. Compute tracking metrics.
4. Log runs to MLflow.
5. Apply the 5-day time box and safest-default rule.

### 5.3 Attribute Procedure

1. Freeze crop extraction and class vocabulary.
2. Replay identical crop sets through both classifiers.
3. Compute overall accuracy, per-color accuracy, and confusion matrix.
4. Log runs to MLflow.
5. Apply the 3-day time box and safest-default rule.

## 6. Reporting Requirements

Every comparison report MUST contain:

- a Markdown ranking table
- matplotlib charts
- an explicit recommendation
- a rationale that references:
  - score
  - deployment fit
  - risk
  - safe-default fallback, when used

The recommendation section MUST use one of these forms:

- `Clear winner: <candidate>`
- `No clear winner after time box; choose safest default: <candidate>`

## 7. Script Contract

### 7.1 `scripts/bakeoff/run_detector_bakeoff.py`

The detector harness MUST:

- validate that the eval dataset exists
- build TensorRT engines from ONNX
- generate Triton model repository entries
- load models into Triton
- run inference against the eval dataset
- compute the detector metrics in §2.4
- compute the score in §2.6
- log the run to MLflow

### 7.2 `scripts/bakeoff/compare_bakeoff.py`

The comparison script MUST:

- read detector runs from MLflow
- produce a Markdown summary
- generate matplotlib charts
- apply the clear-winner rule in §2.7
- emit a recommendation

## 8. Known Constraints

- `data/eval/` is currently empty in this repository, so the harnesses cannot be
  executed successfully yet
- the scripts must therefore optimize for correctness of workflow and failure
  clarity, not immediate execution in this branch
- the Triton model inventory in the repo currently budgets for `yolov8l`; any
  alternative detector winner will require a follow-up placement update if it
  changes runtime assumptions materially

## 9. Acceptance Criteria

### Automated

- `docs/bake-off-protocol.md` exists and does not contain the stub warning
- the document contains the strings `YOLOv8-L`, `YOLOv9-C`, `RT-DETR-L`,
  `ByteTrack`, `BoT-SORT`, and `score =`
- the detector section includes `mAP@0.5`, `mAP@0.5:0.95`, `small_object_AP`,
  and `night_AP`
- the tracker section includes `MOTA` and `IDF1`
- the attribute section includes `confusion matrix`

### Human Review

- the detector protocol is consistent with `docs/triton-placement.md`
- the detector classes match the 7 classes in `docs/taxonomy.md`
- the time-box fallback chooses the least risky default rather than the most
  speculative candidate
- the procedure is concrete enough for an EVAL agent to run once data arrives
