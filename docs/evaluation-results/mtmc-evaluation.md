---
version: "1.0.0"
status: P2-E02
created_by: eval-agent
date: "2026-04-10"
---

# MTMC Re-ID Evaluation

This is a proxy evaluation status report for P2-E02. The real go / no-go
decision for the MTMC service cannot be made yet in this repository state
because the `reid-eval` annotation project has not been exported into a
validated ground-truth file under `data/eval/reid/ground_truth.json`, and the
current `scripts/annotation/export_reid_pairs.py` implementation does not yet
preserve DB-backed `local_track_id` UUIDs in a form that can be joined against
`local_tracks`.

The scripts created in P2-E02 are ready for the real evaluation run:

- `scripts/evaluation/export_reid_gt.py`
- `scripts/evaluation/reid_metrics.py`
- `scripts/evaluation/run_mtmc_eval.py`

The report below documents the method, the decision threshold, and the current
blocking gaps. It does not invent accuracy numbers.

## Method

The evaluation harness measures cross-camera association quality by comparing
annotated identity groups against MTMC output written into:

- `global_tracks`
- `global_track_links`
- `local_tracks`

For each annotated identity group, the harness:

1. loads the set of ground-truth `local_track_id` values
2. queries MTMC assignments from PostgreSQL / TimescaleDB
3. restricts scoring to the annotated track subset so unlabeled site traffic is
   not treated as a false positive
4. computes:
   - Rank-1 accuracy
   - Rank-5 accuracy
   - mean average precision
   - false positive rate
   - false negative rate
   - precision / recall / F1
   - per-camera-pair precision and recall
5. logs metrics and artifacts to MLflow experiment `mtmc-evaluation`

## Go / No-Go Threshold

| Metric | Threshold | Current Status |
|--------|-----------|----------------|
| Rank-1 accuracy | > 70% | Pending real evaluation |

Operational rule:

- if Rank-1 accuracy is greater than `0.70`, the pilot clears the stated go-live
  threshold for MTMC association quality
- if Rank-1 accuracy is less than or equal to `0.70`, the pilot remains
  `NO-GO` until the matcher, topology calibration, or annotation quality is
  improved

## Metrics

| Metric | Value |
|--------|-------|
| Rank-1 accuracy | TBD after real evaluation |
| Rank-5 accuracy | TBD after real evaluation |
| Mean average precision | TBD after real evaluation |
| False positive rate | TBD after real evaluation |
| False negative rate | TBD after real evaluation |
| Precision | TBD after real evaluation |
| Recall | TBD after real evaluation |
| F1 | TBD after real evaluation |
| Total queries | TBD after real evaluation |
| Total true pairs | TBD after real evaluation |
| Total predicted pairs | TBD after real evaluation |

## Per-Camera-Pair Breakdown

| Camera Pair | True Pairs | Predicted Pairs | Correct | Precision | Recall |
|-------------|------------|-----------------|---------|-----------|--------|
| TBD after real evaluation | TBD | TBD | TBD | TBD | TBD |

## Current Gaps

1. `data/eval/reid/ground_truth.json` does not exist in this repo state.
2. The current CVAT export path still drops the DB mapping needed for
   `local_track_id` joins during evaluation.
3. The MTMC service persists final assignments only, not full candidate-ranked
   retrieval lists. That means Rank-1 / Rank-5 / mAP are computed as
   assignment-derived proxies rather than full FAISS retrieval metrics.

## Recommendation

**Cannot make a go / no-go call yet.**

The next steps are:

1. export or repair `reid-eval` ground truth so each sighting contains a real
   DB `local_track_id` UUID
2. run `scripts/evaluation/export_reid_gt.py`
3. run `scripts/evaluation/run_mtmc_eval.py`
4. replace this proxy report with the measured results and final recommendation
