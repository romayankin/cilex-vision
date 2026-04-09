---
version: "1.0.0"
status: P2-A01
created_by: data-agent
date: "2026-04-10"
---

# Annotation Guidelines — Cross-Camera Re-ID

This guide extends the baseline `docs/annotation-guidelines.md` with rules for
labeling cross-camera identity matches. It is the operator reference for the
`reid-eval` CVAT project created by
`scripts/annotation/setup_reid_projects.py` and feeds the MTMC evaluation
harness in P2-E02.

Must stay aligned with:

- `docs/annotation-guidelines.md` (baseline labeling policy)
- `docs/taxonomy.md` (object classes, attributes)
- `services/db/models.py` (canonical enums)
- `services/topology/` (camera graph, transit times)

## 1. Purpose

The `reid-eval` project produces ground-truth identity assignments for
evaluating the MTMC Re-ID Association Service (`services/mtmc-service/`). Each
annotation batch links the **same physical entity** across camera views, so the
evaluation harness can compute Rank-1 accuracy, mAP, and identity switch
metrics.

## 2. CVAT Project

| CVAT Project | Purpose | Annotation Mode | Primary Export |
|--------------|---------|-----------------|----------------|
| `reid-eval` | Cross-camera identity matching | annotation mode | `export_reid_pairs.py` JSON |

Labels: the 7 object classes (`person`, `car`, `truck`, `bus`, `bicycle`,
`motorcycle`, `animal`). Each label carries a free-text attribute called
`global_id` that encodes the cross-camera identity.

## 3. Identity Numbering

Each annotator uses a unique prefix. Identity numbers are sequential within
that prefix:

```
{annotator_prefix}-{global_sequence}
```

Examples:

| Annotator | Prefix | First identity | Second identity |
|-----------|--------|----------------|-----------------|
| Alice | A | A-001 | A-002 |
| Bob | B | B-001 | B-002 |

Rules:

1. The same person/vehicle across **any** camera view gets the **same**
   `global_id`.
2. Never reuse a `global_id` for a different physical entity.
3. Never assign the same entity two different `global_id` values. If you
   discover a duplicate, merge to the lower number and delete the higher one.
4. Prefix letters are assigned before the annotation batch begins and recorded
   in the task description.

## 4. Cross-Camera Pair Labeling

Tasks in `reid-eval` present paired clips or image sets from **adjacent
cameras** with overlapping time windows. The candidate sampler
(`scripts/annotation/sample_reid_candidates.py`) pre-selects plausible
transitions.

Workflow per task:

1. Open the paired camera views side by side.
2. For each visible entity at camera A that also appears at camera B, assign
   the same `global_id` to both sightings.
3. If an entity at camera A has **no match** at camera B, leave its `global_id`
   empty (do not invent a match).
4. If an entity at camera B has **no match** at camera A, leave its `global_id`
   empty.

## 5. Quality Criteria for Re-ID Pairs

A valid identity pair requires **all** of the following:

| Criterion | Requirement |
|-----------|-------------|
| Visibility | Clear view of subject, >50% visible (no heavy occlusion) |
| Temporal plausibility | Entry at camera B within the transit-time window after exit at camera A |
| Class consistency | Same `object_class` in both views |
| Confidence | Source tracks have `mean_confidence > 0.5` |
| Distinguishability | Subject has enough visual features to differentiate from similar-looking entities |

If any criterion fails, do **not** assign the pair.

### 5.1 Temporal Plausibility

Transit-time windows are defined in the topology graph
(`topology_edges` table). The candidate sampler uses the p99 transit
distribution or 3x the baseline transition time as the outer bound. If the
actual transit time between exit at A and entry at B falls outside this window,
the pair is implausible.

### 5.2 Class Consistency

Both sightings must be the same object class. A `person` at camera A cannot be
matched to a `car` at camera B, even if the timestamps align. If you believe
the tracker assigned the wrong class to one sighting, annotate the correct
class in CVAT and note the discrepancy.

## 6. Difficult Cases

### 6.1 Occluded Subjects

- If the subject is >50% occluded in one view but clearly identifiable in the
  other, the pair is still valid — annotate it.
- If the subject is >50% occluded in **both** views, skip the pair.

### 6.2 Similar-Looking Individuals

- Two people wearing identical uniforms (e.g., security guards) must be
  distinguished by physical build, gait context, or accessories.
- If you cannot reliably distinguish them, do **not** assign a match. Leaving
  a pair unmatched is better than a wrong match.

### 6.3 Groups

- When a group moves together between cameras, each member gets their own
  `global_id`. Do not assign a single group identity.
- If individual members cannot be distinguished within the group, skip all
  members.

### 6.4 Vehicles with Similar Appearance

- Two silver sedans of the same model must be distinguished by license plate,
  damage, stickers, or subtle color variation.
- When indistinguishable, skip the pair.

## 7. Review Workflow

Re-ID annotation uses a **dual annotator + adjudicator** model:

1. **Annotator A** labels the batch independently.
2. **Annotator B** labels the same batch independently.
3. **Adjudicator** reviews all cases where A and B disagree:
   - Different `global_id` assigned to the same entity pair → adjudicator picks
     the correct mapping.
   - One annotator matched, the other left empty → adjudicator decides if the
     match is valid.
   - Both annotators left empty → no match (no adjudication needed).
4. Agreement is computed with `export_reid_pairs.py` which reports Cohen's
   kappa on identity assignment.

### 7.1 Agreement Thresholds

| Metric | Target | Escalation |
|--------|--------|------------|
| Identity Cohen's kappa | > 0.80 | < 0.70 triggers batch re-annotation |
| Pair disagreement rate | < 10% | > 15% triggers guideline review |

## 8. Export Format

`scripts/annotation/export_reid_pairs.py` produces:

```json
{
  "pairs": [
    {
      "global_id": "A-001",
      "sightings": [
        {
          "camera_id": "cam-entrance",
          "local_track_id": "...",
          "timestamp": "2026-04-10T10:15:00Z",
          "crop_uri": "s3://datasets/reid/cam-entrance/A-001-0.jpg"
        },
        {
          "camera_id": "cam-lobby",
          "local_track_id": "...",
          "timestamp": "2026-04-10T10:15:32Z",
          "crop_uri": "s3://datasets/reid/cam-lobby/A-001-1.jpg"
        }
      ]
    }
  ],
  "agreement": {
    "identity_kappa": 0.82,
    "pair_count": 150,
    "disagreement_count": 12
  }
}
```

Each entry in `pairs` groups all sightings of one physical entity across
cameras. The `agreement` block summarizes cross-annotator consistency for the
batch.
