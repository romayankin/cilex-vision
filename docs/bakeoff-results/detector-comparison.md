---
version: "1.0.0"
status: P1-E01
created_by: eval-agent
date: "2026-04-06"
---

# Detector Bake-Off Comparison

This is a published-benchmark proxy comparison for P1-E01. The full protocol in
`docs/bake-off-protocol.md` could not be executed here because this environment
does not have a GPU, a live Triton instance, or a populated detector evaluation
set under `data/eval/`.

## Method

The scoring formula still follows the protocol:

```text
score =
  0.35 * mAP@0.5:0.95
  + 0.25 * min(FPS / 40, 1.0)
  + 0.20 * small_object_AP
  + 0.20 * night_AP
```

The proxy inputs differ from a real bake-off run in three important ways:

- `night_AP` is neutralized to the mean overall COCO `mAP@0.5:0.95` across the
  three candidates (`0.5297`) because no primary-source night-slice benchmark
  was found for YOLOv8-L, YOLOv9-C, or RT-DETR-L.
- YOLOv8-L does not expose a primary-source `AP_small` slice in the sources
  found for this task, so its `small_object_AP` term is imputed to the neutral
  median of the published YOLOv9-C and RT-DETR-L `AP_small` values (`0.3535`).
- Target-GPU throughput is inferred, not measured. `docs/triton-placement.md`
  gives the deployment anchor for YOLOv8-L on a 24 GB GPU class (`200-350 FPS`,
  A5000-like to RTX 4090-like). YOLOv9-C and RT-DETR-L are scaled from that
  anchor using official published speed ratios. These are coarse operational
  estimates, not measured Triton latencies.

Because all three candidates remain far above the pilot gate of `40 FPS`
aggregate on the target 24 GB GPU class, the throughput term saturates to `1.0`
for every model. In practice, the proxy ranking is driven by accuracy and the
slice terms, not by speed.

## Comparison

| Rank | Candidate | mAP@0.5 | mAP@0.5:0.95 | `AP_small` used in score | 24 GB Throughput Estimate | 24 GB Latency Estimate | Proxy Score |
|------|-----------|---------|--------------|--------------------------|---------------------------|------------------------|-------------|
| 1 | YOLOv9-C | 0.702 | 0.530 | 0.362 | 226-395 FPS | ~2.5-4.4 ms | 0.6138 |
| 2 | YOLOv8-L | 0.702 | 0.529 | 0.3535 (neutral imputation) | 200-350 FPS | ~2.9-5.0 ms | 0.6118 |
| 3 | RT-DETR-L | 0.714 | 0.530 | 0.345 | 167-292 FPS | ~3.4-6.0 ms | 0.6104 |

Interpretation:

- YOLOv9-C has the highest published proxy score, but the lead over YOLOv8-L is
  only `0.0020`.
- The protocol requires a clear-winner margin of at least `0.0200`.
- That margin is not met, and the missing night / operational-slice data means
  this proxy result is not strong enough to overturn the protocol fallback.

Generated artifacts:

- `docs/bakeoff-results/detector/score_ranking.svg`
- `docs/bakeoff-results/detector/metric_breakdown.svg`

No per-class AP heatmap is attached because primary sources do not publish
per-class AP for the pilot's 7-class taxonomy subset.

## Per-Class Analysis

The protocol requires per-class analysis, but no primary source publishes AP for
the exact 7-class pilot taxonomy (`person`, `car`, `truck`, `bus`, `bicycle`,
`motorcycle`, `animal`). The readout below is therefore a risk-based class
assessment, not measured per-class AP.

- `person`: Crowded scenes, partial occlusions, and night footage matter more
  here than raw COCO mean AP. RT-DETR-L has the strongest published
  architecture story for global context, while YOLOv9-C has the best published
  small-object slice. No source proves a decisive edge on our pilot data.
- `car`: This is the least risky class for all three detectors. Large rigid
  vehicles dominate the easy end of the workload, so deployment stability
  matters more than the tiny proxy-score differences.
- `truck`: Similar to `car`, but with more scale variance and more partial
  occlusion in traffic. None of the published data suggests a material
  difference large enough to justify deployment risk over the current YOLOv8-L
  baseline.
- `bus`: Another large rigid vehicle class. RT-DETR-L's higher published
  `mAP@0.5` hints at strong coarse localization, but the proxy score still does
  not separate the field meaningfully.
- `bicycle`: Thin structures and small instances make this class sensitive to
  `AP_small`. YOLOv9-C has the strongest published small-object signal and is
  the most credible challenger here.
- `motorcycle`: The same thin-object and partial-occlusion issues apply. YOLOv9-C
  again looks best on published small-object evidence, but not by enough to
  overcome the protocol's clear-winner threshold.
- `animal`: This is the least predictable class because pose and silhouette vary
  more than vehicles. RT-DETR-L is the most attractive model on architecture
  grounds for unusual shapes, but there is no published pilot-aligned class AP
  to prove that it actually wins here.

## Recommendation

**Recommend `YOLOv8-L` for the pilot.**

Rationale:

- YOLOv9-C wins the proxy table only by `0.0020`, which is an order of
  magnitude below the protocol's `0.0200` clear-winner rule.
- `night_AP` is missing for all three candidates in the published sources.
- The `0.40` operational slice required by the protocol cannot be measured until
  a real detector evaluation set exists and Triton-backed inference is running.
- `docs/triton-placement.md` is already built around YOLOv8-L, so it remains the
  lowest-risk deployment path when the bake-off is inconclusive.

That means the protocol's day-10 fallback applies: choose the safest default,
`YOLOv8-L`.

## Source Notes

- YOLOv8-L published metrics come from the official Ultralytics model card:
  [docs.ultralytics.com/models/yolov8/](https://docs.ultralytics.com/models/yolov8/)
- YOLOv9-C published metrics come from the official Ultralytics model card and
  the official YOLOv9 README / evaluation block:
  [docs.ultralytics.com/models/yolov9/](https://docs.ultralytics.com/models/yolov9/)
  and
  [raw.githubusercontent.com/WongKinYiu/yolov9/main/README.md](https://raw.githubusercontent.com/WongKinYiu/yolov9/main/README.md)
- The YOLOv8-L vs YOLOv9-C TensorRT speed ratio on Tesla T4 comes from the
  official YOLOv9 repo benchmark issue:
  [github.com/WongKinYiu/yolov9/issues/178](https://github.com/WongKinYiu/yolov9/issues/178)
- RT-DETR-L published metrics come from the official Ultralytics RT-DETR page
  plus the official RT-DETR README:
  [docs.ultralytics.com/models/rtdetr/](https://docs.ultralytics.com/models/rtdetr/)
  and
  [raw.githubusercontent.com/lyuwenyu/RT-DETR/main/README.md](https://raw.githubusercontent.com/lyuwenyu/RT-DETR/main/README.md)
- RT-DETR-L `AP_small` is inferred from the official RT-DETR COCO training-log
  attachment for the matching HGNetv2-L checkpoint:
  [github.com/lyuwenyu/RT-DETR/issues/8](https://github.com/lyuwenyu/RT-DETR/issues/8)
- Target-GPU deployment anchors come from the local placement spec:
  `docs/triton-placement.md`
