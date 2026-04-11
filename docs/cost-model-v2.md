# Cost Model v2 — Measured Parameters

Updated cost projections using parameters measured from the 4-camera pilot deployment, stress tests, and Phase 2 infrastructure work. Replaces the original estimates from P0-X01.

---

## Executive Summary

Pilot measurements show the original cost estimates were **conservative**: actual workload is 20-36% lighter than estimated across key parameters. Combined with the 7-day frame blob retention (vs 30 days estimated), monthly costs at 100 cameras are approximately **25-35% lower** than the v1 model projected.

Three new cost categories are now modeled:

- **MTMC infrastructure** — Re-ID GPU share, FAISS memory, checkpoint storage
- **Annotation pipeline** — CVAT hosting, hard-example mining, annotator time
- **Shadow deployment overhead** — GPU and Kafka overhead during model rollout (amortized)

The platform remains **compute-bound, not storage-bound**: GPU and fixed infrastructure account for 75-85% of total cost across all scenarios.

---

## Parameter Comparison

Six parameters changed materially from the original P0-X01 estimates:

| Parameter | Estimated | Measured | Delta | Source |
|-----------|-----------|----------|-------|--------|
| Motion duty cycle P25 | 8% | 6% | -25.0% | pilot-measured |
| Motion duty cycle P50 | 15% | 12% | -20.0% | pilot-measured |
| Motion duty cycle P90 | 35% | 30% | -14.3% | pilot-measured |
| Detections per frame | 5.0 | 3.2 | -36.0% | pilot-measured |
| Active tracks per camera | 5.0 | 4.0 | -20.0% | pilot-measured |
| Frame blob retention | 30 days | 7 days | -76.7% | pilot-measured |

**Why these changed:**

- **Motion duty cycle**: The pilot site has less ambient motion than assumed. Scenes are quieter, so the motion filter suppresses more frames. This directly reduces inference load and storage.
- **Detections per frame**: Fewer simultaneous objects per frame than the conservative estimate. Reduces Kafka message volume, TimescaleDB row counts, and attribute pipeline load.
- **Active tracks per camera**: Follows from lower detection density. Fewer concurrent tracks mean less MTMC matching work and fewer embedding updates.
- **Frame blob retention**: The lifecycle policy (`infra/minio/lifecycle-policies.json`) expires frame blobs after 7 days, not 30. This was a design decision from P2-O02 (storage tiering), not a measurement change.

All other parameters (bitrate, inference FPS, GPU sizing, database row sizes, compression ratios, service costs) were **confirmed** by the pilot at their estimated values.

---

## Updated Cost Projections

Camera counts updated to 10, 50, and 100 (removed 4-camera pilot tier, added 50-camera mid-scale).

### P50 Scenario (12% motion duty cycle) — Most Likely

| Component | 10 cameras | 50 cameras | 100 cameras |
|-----------|-----------|-----------|------------|
| Fixed infrastructure | $1,085 | $1,085 | $1,085 |
| GPU | $1,200 | $1,200 | $1,200 |
| Hot object storage | $8 | $42 | $83 |
| Warm object storage | <$1 | <$1 | <$1 |
| Cold object storage | <$1 | <$1 | $1 |
| Kafka storage | $1 | $4 | $8 |
| TimescaleDB storage | $2 | $8 | $16 |
| MTMC infrastructure | $120 | $120 | $120 |
| Annotation pipeline | $450 | $450 | $450 |
| Shadow overhead | $60 | $60 | $60 |
| **Total monthly** | **$2,926** | **$2,969** | **$3,023** |

### P90 Scenario (30% motion duty cycle) — High Activity

| Component | 10 cameras | 50 cameras | 100 cameras |
|-----------|-----------|-----------|------------|
| Fixed infrastructure | $1,085 | $1,085 | $1,085 |
| GPU | $1,200 | $1,200 | $2,400 |
| Hot object storage | $21 | $104 | $209 |
| MTMC infrastructure | $120 | $120 | $240 |
| Annotation pipeline | $450 | $450 | $450 |
| Shadow overhead | $60 | $60 | $121 |
| Other storage | $6 | $31 | $62 |
| **Total monthly** | **$2,942** | **$3,051** | **$4,566** |

Key observation: 100 cameras at P90 is the first scenario requiring 2 GPU nodes, which nearly doubles GPU cost.

---

## New Cost Categories

### MTMC Infrastructure

| Component | Monthly Cost | Notes |
|-----------|-------------|-------|
| Re-ID GPU fraction | 10% of GPU cost | OSNet inference share of Triton GPU |
| FAISS memory | ~50 MB per 10k embeddings | Informational; included in server RAM |
| Checkpoint storage | ~100 MB steady state | mtmc-checkpoints bucket, 7-day hot tier |

MTMC infrastructure adds $120/month per GPU node. This is dominated by the Re-ID GPU share — FAISS memory and checkpoint storage are negligible in dollar terms.

### Annotation Pipeline

| Component | Monthly Cost | Notes |
|-----------|-------------|-------|
| CVAT hosting | $50 | Container-based, single instance |
| Hard-example mining compute | $25 | Daily query against debug traces |
| Annotator time | $375 | $25/hr x 0.5 hr/day x 30 days |
| **Total** | **$450** | Fixed cost, independent of camera count |

Annotation is a flat $450/month regardless of camera count. This is the largest new cost category and is dominated by annotator time. Consider reducing `daily_mining_hours` if annotation throughput exceeds retraining needs.

### Shadow Deployment

| Component | Per-Rollout Cost | Amortized Monthly |
|-----------|-----------------|-------------------|
| GPU overhead | +15% of GPU cost | ~$60 (1 GPU) |
| Kafka overhead | +10% of Kafka storage cost | <$1 |

Shadow deployment overhead is amortized at 1 rollout month per quarter (33%). Actual cost depends on rollout frequency. At 100 cameras / P90 with 2 GPU nodes, amortized shadow cost doubles to ~$121/month.

---

## Storage Tiering Breakdown

The v2 model splits object storage into three tiers matching the MinIO lifecycle policies:

| Tier | Buckets | Retention | Rate/GB-month | 100-cam P50 Volume |
|------|---------|-----------|---------------|-------------------|
| Hot | frame-blobs, decoded-frames, mtmc-checkpoints | 7 days | $0.023 | 3.6 TB |
| Warm | event-clips, thumbnails, archive-warm | 30-90 days | $0.0125 | 15 GB |
| Cold | debug-traces, raw-video | 30 days | $0.0125 | 62 GB |

The 7-day hot retention (vs 30-day in v1) is the single largest cost reduction: **~77% less hot storage** compared to the original model.

---

## Recommendations

1. **GPU is the scaling bottleneck.** At P50, a single 24GB GPU handles up to 100 cameras. At P90 with 100 cameras, a second GPU is required. GPU cost doubles discretely — plan GPU additions at the 32-camera-equivalent threshold.

2. **Annotation cost is fixed and significant.** At $450/month, annotation is the third-largest cost after GPU and fixed infrastructure. If you are not actively retraining, reduce `daily_mining_hours` or pause the mining cron.

3. **Storage costs are negligible.** Combined hot + warm + cold + Kafka + TimescaleDB storage is under $110/month even at 100 cameras / P50. Storage tiering works: the 7-day hot retention is the right call.

4. **Validate duty cycle at each new site.** The 12% P50 duty cycle was measured at the pilot site. Higher-traffic sites (transit hubs, intersections) may hit 30%+ sustained, pushing GPU scaling earlier.

5. **Run `cost_model_v2.py` before each deployment.** Update `params-measured.yaml` with site-specific measurements to get accurate cost projections:
   ```bash
   python3 scripts/cost-model/cost_model_v2.py --skip-xlsx
   ```

---

## Files

| File | Purpose |
|------|---------|
| `scripts/cost-model/params-measured.yaml` | Measured parameters with provenance |
| `scripts/cost-model/cost_model_v2.py` | Cost model script (generates tables + Excel) |
| `scripts/cost-model/params.yaml` | Original v1 estimates (preserved for reference) |
| `scripts/cost-model/cost_model.py` | Original v1 cost model script |
| `artifacts/cost-model/cost-model-v2.xlsx` | Generated Excel workbook (run script to produce) |
