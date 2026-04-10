---
version: "1.0.0"
status: P2-E01
created_by: eval-agent
date: "2026-04-10"
---

# Attribute Bake-Off Comparison

This is a proxy recommendation for P2-E01. The real attribute bake-off from
`docs/bake-off-protocol.md` cannot be executed yet in this repository state
because the required evaluation set under `data/eval/attribute/manifest.json`
does not exist, no committed EfficientNet-B0 export is present under
`artifacts/models/attribute/`, and no MLflow-backed candidate runs have been
recorded yet.

That means the report below is intentionally conservative: it documents the
decision rule, the current deployment baseline, and the reasons the protocol
fallback applies today. It is not a substitute for a real run of
`scripts/bakeoff/prepare_color_eval_data.py` plus two runs of
`scripts/bakeoff/run_attribute_bakeoff.py`.

## Method

The comparison still follows the attribute decision rule from
`docs/bake-off-protocol.md`:

1. highest macro per-color accuracy
2. lowest confusion among:
   - black vs blue
   - white vs silver
   - brown vs orange
3. lowest latency at equal accuracy, when the latency gap is operationally
   meaningful

The current repo can only support a protocol-readiness proxy:

- `ResNet-18` is the deployed baseline already budgeted in
  `infra/triton/models/color_classifier/config.pbtxt` and
  `docs/triton-placement.md`.
- `EfficientNet-B0` is a valid challenger family in the protocol, but this repo
  does not yet contain a committed ONNX export, TensorRT engine, Triton config
  variant, or measured bake-off run for it.
- No trustworthy public benchmark in this repo maps directly to the exact
  10-color vocabulary (`red`, `blue`, `white`, `black`, `silver`, `green`,
  `yellow`, `brown`, `orange`, `unknown`) plus the service-specific
  preprocessing and `unknown` thresholding policy.

## Comparison

| Candidate | Overall Accuracy | Macro Per-Color Accuracy | Unknown Rate | Operational Confusions | Latency / Throughput | Deployment Fit |
|-----------|------------------|--------------------------|--------------|------------------------|----------------------|----------------|
| ResNet-18 | Pending real evaluation | Pending real evaluation | Pending real evaluation | Pending real evaluation | Existing repo-local Triton budget only; no live bake-off run yet | Baseline already wired as `color_classifier` |
| EfficientNet-B0 | Pending real evaluation | Pending real evaluation | Pending real evaluation | Pending real evaluation | No committed repo-local benchmark or export artifact | Additional export, deployment, and latency validation required |

Interpretation:

- There is no measured winner because the required attribute evaluation set is
  still absent.
- The protocol does not allow a recommendation to be invented from unrelated
  classification benchmarks or generic ImageNet scores.
- The only defensible decision in the current state is the documented safe
  default.

## Per-Color Accuracy

The protocol requires per-color accuracy for all 10 colors. That table cannot
be populated honestly until `prepare_color_eval_data.py` exports a complete
color crop manifest and both candidates are run through
`run_attribute_bakeoff.py`.

| Color | ResNet-18 | EfficientNet-B0 |
|-------|-----------|-----------------|
| red | TBD after real evaluation | TBD after real evaluation |
| blue | TBD after real evaluation | TBD after real evaluation |
| white | TBD after real evaluation | TBD after real evaluation |
| black | TBD after real evaluation | TBD after real evaluation |
| silver | TBD after real evaluation | TBD after real evaluation |
| green | TBD after real evaluation | TBD after real evaluation |
| yellow | TBD after real evaluation | TBD after real evaluation |
| brown | TBD after real evaluation | TBD after real evaluation |
| orange | TBD after real evaluation | TBD after real evaluation |
| unknown | TBD after real evaluation | TBD after real evaluation |

## Operational Confusion Analysis

The protocol singles out three confusion pairs because they are operationally
costly in surveillance video:

- `black ↔ blue`
- `white ↔ silver`
- `brown ↔ orange`

No confusion matrix is available yet, so none of those pairwise confusion rates
can be compared. The new harness is built to compute and log:

- `confusion_black_blue`
- `confusion_white_silver`
- `confusion_brown_orange`

until those metrics exist in MLflow, the confusion ranking step remains
unresolved.

## Latency Comparison

Latency is also unresolved at the bake-off level:

- `ResNet-18` has an existing deployment budget in `docs/triton-placement.md`
  because the current `color_classifier` model is already assumed to be a
  lightweight FP16 baseline.
- `EfficientNet-B0` has no repo-local Triton or ONNXRuntime timing artifact in
  this branch.

The new harness records `latency_p50_ms`, `latency_p95_ms`, `latency_p99_ms`,
and `throughput_fps` so the real bake-off can settle this point once data and
artifacts exist.

## Recommendation

**No clear winner after time box; choose safest default: `ResNet-18`.**

Rationale:

- the protocol explicitly names `ResNet-18` as the safe default when the
  3-day bake-off does not produce a clear winner
- the repo already deploys `ResNet-18` as the baseline `color_classifier`
- there is no measured evidence in this repo that `EfficientNet-B0` improves
  macro per-color accuracy or the operational confusion pairs enough to justify
  extra deployment risk

Follow-up required for a real decision:

1. export the `attribute-eval` CVAT project with
   `scripts/bakeoff/prepare_color_eval_data.py`
2. run `scripts/bakeoff/run_attribute_bakeoff.py` for `resnet18`
3. run `scripts/bakeoff/run_attribute_bakeoff.py` for `efficientnet_b0`
4. compare the two MLflow runs against the protocol ranking order
