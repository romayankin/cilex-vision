# 50-100 Camera Load Test Report

## Test Configuration

- Camera count: `TBD`
- Duration: `TBD`
- Snapshot interval: `TBD`
- Prometheus: `TBD`
- Query API: `TBD`
- Probe camera: `TBD`
- DB probe enabled: `TBD`
- Snapshot count: `TBD`

## Scenario Requirements

| Requirement | Target | Measured | Result | Notes |
| --- | --- | --- | --- | --- |
| Sustained duration | >= 14,400 s | `TBD` | `TBD` | Phase 4 scale tests must hold sustained load for at least 4 hours. |
| Scale target | 50-100 cameras | `TBD` | `TBD` | Configured camera fan-out for the replay workload. |
| Chaos coverage | >= 1 executed scenario | `TBD` | `TBD` | Scale sign-off requires at least one executed chaos scenario. |

## NFR Pass/Fail

| NFR | Target | Measured | Result | Notes |
| --- | --- | --- | --- | --- |
| End-to-end latency (p95) | < 2,000 ms | `TBD` | `TBD` | Use canonical `e2e_latency_ms`; document any fallback explicitly. |
| Inference throughput | 5-10 FPS per camera | `TBD` | `TBD` | Derive from `inference_frames_consumed_total / camera_count`. |
| Query latency (p95) | < 500 ms | `TBD` | `TBD` | Prefer direct Query API probes alongside Prometheus. |
| Kafka consumer lag | < 10,000 messages | `TBD` | `TBD` | Use canonical `kafka_consumer_lag` when available. |
| System availability | >= 99.5% | `TBD` | `TBD` | Average Prometheus `up` across the run. |

## Throughput Achieved vs Target

| Metric | Average | Peak |
| --- | --- | --- |
| Frames in / s | `TBD` | `TBD` |
| Frames decoded / s | `TBD` | `TBD` |
| Inference frames / s | `TBD` | `TBD` |
| Detections / s | `TBD` | `TBD` |
| Events / s | `TBD` | `TBD` |
| MTMC matches / s | `TBD` | `TBD` |
| Query requests / s | `TBD` | `TBD` |
| Bulk rows / s | `TBD` | `TBD` |
| FPS / camera | `TBD` | `TBD` |
| Active tracks / camera | `TBD` | `TBD` |

## Latency Percentiles per Stage

| Stage | p50 | p95 | p99 | Source |
| --- | --- | --- | --- | --- |
| Ingest / end-to-end latency | `TBD` | `TBD` | `TBD` | `TBD` |
| Decode latency | `TBD` | `TBD` | `TBD` | `TBD` |
| Inference latency | `TBD` | `TBD` | `TBD` | `TBD` |
| Embedding latency | `TBD` | `TBD` | `TBD` | `TBD` |
| DB write latency | `TBD` | `TBD` | `TBD` | `TBD` |
| Query API latency (Prometheus) | `TBD` | `TBD` | `TBD` | `TBD` |
| MTMC match latency | `TBD` | `TBD` | `TBD` | `TBD` |

## Direct Query API Probes

| Endpoint | p50 | p95 | p99 | Average |
| --- | --- | --- | --- | --- |
| /detections | `TBD` | `TBD` | `TBD` | `TBD` |
| /tracks | `TBD` | `TBD` | `TBD` | `TBD` |
| /events | `TBD` | `TBD` | `TBD` | `TBD` |

## Resource Utilization — CPU and RAM

| Service | Avg CPU (cores) | Peak CPU (cores) | Avg RAM | Peak RAM |
| --- | --- | --- | --- | --- |
| `TBD` | `TBD` | `TBD` | `TBD` | `TBD` |

## Resource Utilization — Disk and Network

| Service | Avg Disk | Peak Disk | Avg RX | Peak RX | Avg TX | Peak TX |
| --- | --- | --- | --- | --- | --- | --- |
| `TBD` | `TBD` | `TBD` | `TBD` | `TBD` | `TBD` | `TBD` |

## Resource Utilization — GPU

| Metric | Average | Peak |
| --- | --- | --- |
| GPU utilization | `TBD` | `TBD` |
| GPU memory used | `TBD` | `TBD` |
| GPU memory total | `TBD` | `TBD` |
| GPU count | `TBD` | `TBD` |

## Storage Growth and Bucket Footprint

| Bucket | Latest Size | Growth / day | Notes |
| --- | --- | --- | --- |
| `TBD` | `TBD` | `TBD` | `TBD` |

## Kafka Consumer Lag

| Group / Topic | Max Lag |
| --- | --- |
| `TBD` | `TBD` |

## Chaos Recovery Times

| Scenario | Target | Status | Recovery Time | Data Loss | Notes |
| --- | --- | --- | --- | --- | --- |
| `TBD` | `TBD` | `TBD` | `TBD` | `TBD` | `TBD` |

## Bottleneck Identification

- Slowest observed stage: `TBD`
- Highest CPU consumer: `TBD`
- Highest RAM consumer: `TBD`
- Peak Kafka lag observed: `TBD`

## Cost Model Comparison

| Parameter | Predicted | Actual / Observed | Delta | Notes |
| --- | --- | --- | --- | --- |
| Monthly platform cost | `TBD` | `TBD` | `TBD` | Billing export required for a true actual-vs-predicted cost comparison. |
| Inference FPS / camera | `TBD` | `TBD` | `TBD` | `TBD` |
| Active tracks / camera | `TBD` | `TBD` | `TBD` | `TBD` |
| Cameras / GPU | `TBD` | `TBD` | `TBD` | `TBD` |
| Hot storage steady state | `TBD` | `TBD` | `TBD` | `TBD` |

## Recommendations

- `TBD`

## Notes

- Missing metrics remain explicit `FAIL` conditions rather than silent passes.
- Query API latency should be reported from both Prometheus histograms and direct HTTP probes.
- Cost validation should compare the run against the measured P3 cost-model drivers, not only against a flat monthly dollar total.
