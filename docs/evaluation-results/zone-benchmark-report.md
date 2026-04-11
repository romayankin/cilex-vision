# Zone Benchmark Report

## Configuration Summary

- Dataset: `data/eval/zone-benchmark/dataset.json`
- Cameras: `TBD`
- Identities: `TBD`
- Cross-zone fraction: `TBD`
- Embedding dimension: `512`
- MLflow run ID: `TBD`

## Accuracy Comparison

| Scenario | Zones | Cameras/Zone | Rank-1 | Rank-5 | mAP | Precision | Recall | FPR | Delta vs Full Rank-1 |
|----------|-------|--------------|--------|--------|-----|-----------|--------|-----|----------------------|
| full-site | TBD | TBD | TBD | TBD | TBD | TBD | TBD | TBD | baseline |
| zone-10 | TBD | 10 | TBD | TBD | TBD | TBD | TBD | TBD | TBD |
| zone-25 | TBD | 25 | TBD | TBD | TBD | TBD | TBD | TBD | TBD |
| zone-50 | TBD | 50 | TBD | TBD | TBD | TBD | TBD | TBD | TBD |

## Cross-Zone Boundary Accuracy

| Scenario | Boundary Identity Groups | Rank-1 | Rank-5 | mAP | Precision | Recall |
|----------|--------------------------|--------|--------|-----|-----------|--------|
| zone-10 | TBD | TBD | TBD | TBD | TBD | TBD |
| zone-25 | TBD | TBD | TBD | TBD | TBD | TBD |
| zone-50 | TBD | TBD | TBD | TBD | TBD | TBD |

## Latency Scaling

| Scenario | Mean Search ms | p50 ms | p95 ms | p99 ms | Avg Candidates/Query |
|----------|----------------|--------|--------|--------|----------------------|
| full-site | TBD | TBD | TBD | TBD | TBD |
| zone-10 | TBD | TBD | TBD | TBD | TBD |
| zone-25 | TBD | TBD | TBD | TBD | TBD |
| zone-50 | TBD | TBD | TBD | TBD | TBD |

Latency scaling curves:
- Replace this section with exported charts or MLflow screenshots once a real run is complete.

## Memory Scaling

| Scenario | Peak Total Index | Peak Largest Shard | Peak Boundary Index | Peak Largest Shard vs Full |
|----------|------------------|--------------------|---------------------|----------------------------|
| full-site | TBD | TBD | TBD | baseline |
| zone-10 | TBD | TBD | TBD | TBD |
| zone-25 | TBD | TBD | TBD | TBD |
| zone-50 | TBD | TBD | TBD | TBD |

## Recommendations

- No recommendation yet. Run `scripts/evaluation/zone_benchmark.py`, review the MLflow run, and replace `TBD` values with measured results.
- Gate suggestion: keep sharding only if Rank-1 and mAP stay within 2 percentage points of full-site while p95 search latency improves materially.

## Notes

- This report is a placeholder template committed before a real benchmark run.
- The benchmark harness logs to MLflow experiment `zone-benchmark`.
- Boundary accuracy is expected to be the most sensitive metric because cross-zone matching uses the relaxed 0.55 threshold and boundary-camera-only search.
