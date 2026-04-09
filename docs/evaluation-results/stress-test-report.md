# End-to-End Stress Test Report

## Test Configuration

- Duration: `3600 s`
- Cameras: `4`
- Target FPS per camera: `5`
- Query load target: `10 QPS`
- Prometheus: `http://localhost:9090`
- Query API: `http://localhost:8080`
- Chaos enabled: `false`
- Replay media: `set --replay-frame-dir for event-heavy runs`

## NFR Pass/Fail

| NFR | Target | Measured | Result | Notes |
| --- | --- | --- | --- | --- |
| End-to-end latency (p95) | < 2,000 ms | `TBD` | `TBD` | Populate from `e2e_latency_ms` or mark fail if absent. |
| Inference throughput | 5-10 FPS per camera | `TBD` | `TBD` | Use `inference_fps{camera_id}` when available; otherwise document the fallback used. |
| Pilot cameras | 4 cameras | `TBD` | `TBD` | Validate against the deployment health contract in use. |
| Query latency (p95) | < 500 ms | `TBD` | `TBD` | Populate from `query_latency_ms`. |
| Kafka consumer lag | < 10,000 messages | `TBD` | `TBD` | Prefer canonical `kafka_consumer_lag{group,topic}`. |

## Per-Stage Latency

| Stage | p50 | p95 | p99 |
| --- | --- | --- | --- |
| End-to-end latency | `TBD` | `TBD` | `TBD` |
| Inference latency | `TBD` | `TBD` | `TBD` |
| Embedding latency | `TBD` | `TBD` | `TBD` |
| Attribute classification latency | `TBD` | `TBD` | `TBD` |
| Event DB write latency | `TBD` | `TBD` | `TBD` |
| Clip extraction latency | `TBD` | `TBD` | `TBD` |
| Query latency | `TBD` | `TBD` | `TBD` |

## Throughput

| Metric | Average | Peak |
| --- | --- | --- |
| Detections / s | `TBD` | `TBD` |
| Events / s | `TBD` | `TBD` |
| MTMC matches / s | `TBD` | `TBD` |
| Queries / s | `TBD` | `TBD` |
| Clips / s | `TBD` | `TBD` |
| Bulk rows / s | `TBD` | `TBD` |
| Embeddings / s | `TBD` | `TBD` |

## Resource Utilization

| Service | Avg CPU (cores) | Peak CPU (cores) | Avg RAM | Peak RAM |
| --- | --- | --- | --- | --- |
| `TBD` | `TBD` | `TBD` | `TBD` | `TBD` |

## Kafka Consumer Lag

| Group / Topic | Max Lag |
| --- | --- |
| `TBD` | `TBD` |

## Chaos Scenarios

| Scenario | Target | Success | Recovery Time | Data Loss | Notes |
| --- | --- | --- | --- | --- | --- |
| `TBD` | `TBD` | `TBD` | `TBD` | `TBD` | `TBD` |

## Bottleneck Analysis

- Slowest observed stage: `TBD`
- Highest CPU consumer: `TBD`
- Highest RAM consumer: `TBD`
- Follow-up action: `TBD`

## Cost Model Comparison

| Parameter | Predicted | Actual / Observed | Delta |
| --- | --- | --- | --- |
| Inference FPS per camera | `TBD` | `TBD` | `TBD` |
| Active tracks per camera | `TBD` | `TBD` | `TBD` |
| Motion duty cycle | `TBD` | `TBD` | `TBD` |

## Notes

- The committed harness uses synthetic JPEG generation by default so it can exercise the live pipeline without bundled media.
- For realistic event, clip, and MTMC traffic, provide a replay directory with representative images via `--replay-frame-dir`.
- Missing observability metrics should be treated as failed validation, not silent pass conditions.
