# Monitoring Guide

Dashboard-by-dashboard reference for all 8 Grafana dashboards and a complete alert rule index. For each dashboard: what it shows, what to look for, and when to escalate.

---

## Dashboards

### 1. Stream Health

**UID:** `stream-health` | **URL:** `/d/stream-health`

<!-- screenshot placeholder: stream-health -->

| Panel | Type | What it shows |
|-------|------|---------------|
| Camera Uptime % | stat | Per-camera uptime ratio. Target: >99%. |
| Active Cameras | stat | Count of cameras currently streaming. Must match deployed count. |
| Edge Buffer Fill | gauge | Local edge buffer utilization. Above 80% = frames may be dropped. |
| Decode Errors / min | timeseries | Frame decode failures. Should be near zero. |
| NATS Publish Latency p95 (ms) | timeseries | Edge-to-NATS publish latency. Target: <50ms. |
| Motion Duty Cycle | timeseries | Fraction of time with motion detected per camera. |
| Motion vs Static Frames / sec | timeseries | Ratio of forwarded vs filtered frames. Expect ~15% pass-through. |

**What to look for:**

- A camera with 0% uptime means the RTSP feed is unreachable.
- Edge buffer fill above 80% combined with high NATS latency suggests network congestion between edge and center.
- Decode errors often indicate codec mismatch (e.g., H.265 stream but only H.264 decoder available).
- Motion duty cycle near 0% on a camera that should have activity suggests the motion filter threshold is too aggressive.

**When to escalate:**

- Camera offline for >15 minutes after restart attempt.
- Edge buffer fill at 100% (frames are being dropped).

**Runbook:** [Incident Response — Stream Alerts](../runbooks/incident-response.md), [Camera Onboarding](../runbooks/camera-onboarding.md)

---

### 2. Inference Performance

**UID:** `inference-performance` | **URL:** `/d/inference-performance`

<!-- screenshot placeholder: inference-performance -->

| Panel | Type | What it shows |
|-------|------|---------------|
| Frames Consumed / sec | timeseries | Rate of frames entering the inference worker from Kafka. |
| Detections / sec by Class | timeseries | Detection output rate broken down by object class. |
| Detection Inference Latency (ms) | timeseries | YOLOv8-L inference latency (p50/p95/p99). Target p99: <100ms. |
| Embedding Inference Latency (ms) | timeseries | OSNet Re-ID embedding latency (p50/p95/p99). Target p99: <50ms. |
| GPU VRAM Usage % | gauge | Current VRAM utilization. 3 models use ~600MB on 24GB GPU. |
| Triton Queue Delay (ms) | timeseries | Time requests spend waiting in the Triton inference queue. |
| Active Tracks per Camera | timeseries | Current number of active ByteTrack tracks per camera. |
| Tracks Closed / min | timeseries | Rate of track closures (object left scene or tracking lost). |
| Consumer Lag (Inference Worker) | timeseries | Kafka consumer lag for the inference worker group. |

**What to look for:**

- Consumer lag growing over time means the inference worker cannot keep up with the frame rate.
- VRAM above 90% risks OOM. Check if shadow models are loaded alongside production models.
- Triton queue delay above 50ms means the GPU is saturated. Consider adding GPU capacity or reducing frame rate.
- A sudden drop in detections with stable frame consumption suggests a model loading failure.

**When to escalate:**

- Consumer lag growing for >10 minutes.
- VRAM above 90% with no shadow deployment active.
- Zero detections output with frames being consumed.

**Runbook:** [Incident Response — Inference Alerts](../runbooks/incident-response.md), [Scaling — Adding GPU Nodes](../runbooks/scaling.md)

---

### 3. Bus Health

**UID:** `bus-health` | **URL:** `/d/bus-health`

<!-- screenshot placeholder: bus-health -->

| Panel | Type | What it shows |
|-------|------|---------------|
| Kafka Consumer Lag (Bulk Collector) | timeseries | Lag on the bulk-collector consumer group. |
| Messages Consumed / sec (Bulk Collector) | timeseries | Consumption throughput. |
| NATS Consumer Lag (Bridge) | timeseries | Lag on the ingress bridge's NATS subscription. |
| Bridge Throughput (msg/s) | timeseries | Messages flowing through the ingress bridge. |
| Bridge Spool Fill % | gauge | NVMe spool utilization. Fills when Kafka is unavailable. |
| Spool Depth (messages) | timeseries | Number of messages queued in the spool. |
| Bridge NATS->Kafka Latency p95 (ms) | timeseries | End-to-end latency through the bridge. |
| Schema Rejections & DLQ / min | timeseries | Messages failing schema validation or sent to dead-letter queue. |
| Kafka Producer In-Flight & Produce Latency | timeseries | Kafka producer behavior. |

**What to look for:**

- Spool fill above 50% means Kafka is slow or unreachable. The spool has 50GB capacity.
- Schema rejections indicate a protobuf version mismatch between edge and center.
- DLQ messages mean a message failed 3 retries and was dead-lettered. These need manual review.
- Consumer lag on bulk-collector means writes to TimescaleDB are falling behind.

**When to escalate:**

- Spool fill above 80% and rising (`BridgeSpoolFillCritical` alert).
- Schema rejection rate above 0 for >5 minutes.
- Kafka consumer lag rising on bulk-collector with no obvious DB pressure.

**Runbook:** [Incident Response — Bus Alerts](../runbooks/incident-response.md)

---

### 4. Storage

**UID:** `storage` | **URL:** `/d/storage`

<!-- screenshot placeholder: storage -->

| Panel | Type | What it shows |
|-------|------|---------------|
| Rows Written / sec by Table | timeseries | Write rate to `detections` and `track_observations` tables. |
| Write Latency p99 (ms) | timeseries | TimescaleDB COPY write latency. Target: <50ms. |
| Write Errors / min | timeseries | Failed write operations. Must be zero. |
| Rows Currently Staged | timeseries | Rows buffered in the bulk collector awaiting flush. |
| Batch Size (avg rows per flush) | timeseries | Average COPY batch size. |
| Duplicates Skipped / min | timeseries | Rows rejected by ON CONFLICT deduplication. |
| Messages Rejected / min | timeseries | Messages dropped before write attempt. |
| Decode Service Throughput | timeseries | Frame decode output rate. |
| Decode Consumer Lag | timeseries | Lag on the decode service Kafka consumer. |

**What to look for:**

- Write errors are the most critical signal. Any non-zero value means data is being lost.
- Write latency spikes during compression or retention policy execution are expected but should resolve within minutes.
- A growing staged-rows count means the collector's flush cycle cannot keep pace.
- Decode consumer lag indicates the decode service is falling behind.

**When to escalate:**

- Write errors sustained for >5 minutes.
- Staged rows above 50,000 and growing.
- Write latency p99 above 500ms sustained.

**Runbook:** [Incident Response — Storage Alerts](../runbooks/incident-response.md), [Backup and Restore](../runbooks/backup-restore.md)

---

### 5. Model Quality

**UID:** `model-quality` | **URL:** `/d/model-quality`

<!-- screenshot placeholder: model-quality -->

| Panel | Type | What it shows |
|-------|------|---------------|
| Detection Count per Class / hour | timeseries | Hourly detection volume by object class. |
| Total Detections by Class (last 1h) | bargauge | Bar chart of class distribution in the last hour. |
| Detection Rate by Camera / min | timeseries | Per-camera detection rate for spot-checking individual cameras. |
| Active Tracks per Camera | timeseries | Concurrent tracked objects per camera. |
| Track Close Rate / min (ID switch proxy) | timeseries | Rate of track terminations — proxy for ID switch frequency. |
| Track Turnover Ratio | timeseries | Ratio of closures to openings. Stable ~1.0 is healthy. |
| Inference Pipeline Throughput | timeseries | End-to-end pipeline throughput. |
| Publish Errors by Topic | timeseries | Failed publishes to Kafka topics. Must be zero. |

**What to look for:**

- A class showing zero detections during hours when that class should be active (e.g., "person" during business hours) suggests a model or threshold problem.
- Track turnover significantly above 1.0 indicates frequent tracker resets or ID switches.
- A camera with detection rate far below others in similar environments may have a physical obstruction or exposure issue.

**When to escalate:**

- Any class at zero detections for >30 minutes during expected activity.
- Track turnover ratio doubles compared to the 7-day baseline.
- Publish errors on any topic.

---

### 6. MTMC Re-ID Health

**UID:** `mtmc-health` | **URL:** `/d/mtmc-health`

<!-- screenshot placeholder: mtmc-health -->

| Panel | Type | What it shows |
|-------|------|---------------|
| Match Rate | timeseries | Fraction of embedding comparisons resulting in a cross-camera match. |
| Reject Rate | timeseries | Fraction of comparisons rejected (below 0.65 threshold). |
| Match Score Distribution | heatmap | Distribution of FAISS similarity scores for matches. |
| FAISS Index Size | timeseries | Number of embeddings in the live FAISS index. |
| Checkpoint Lag | gauge | Seconds since last successful checkpoint write. |
| Checkpoint Size | gauge | Size of the FAISS checkpoint in bytes. |
| Rebalance Events | timeseries | FAISS index flush/rebuild events (model cutover, ADR-008). |
| Embeddings Consumed | timeseries | Rate of embedding ingestion from Kafka. |

**What to look for:**

- Match rate baseline varies by site. Establish a baseline during the first week and compare daily.
- Checkpoint lag above 300s means the FAISS state may not survive a restart without data loss.
- FAISS index size should grow and decay predictably. Unbounded growth suggests the 30-minute eviction window is not running.
- A rebalance event indicates a model version cutover (ADR-008). Expect ~30s matching blackout.

**When to escalate:**

- Match rate drops to zero.
- Checkpoint lag above 300s (`MtmcCheckpointLagCritical`).
- FAISS index size exceeds expected bounds (`MtmcFaissIndexAnomaly`).

---

### 7. Shadow vs Production Comparison

**UID:** `shadow-comparison` | **URL:** `/d/shadow-comparison`

<!-- screenshot placeholder: shadow-comparison -->

| Panel | Type | What it shows |
|-------|------|---------------|
| Detection Count: Production vs Shadow | timeseries | Side-by-side detection counts from production and shadow workers. |
| Confidence Distribution | timeseries | Shadow model confidence percentiles (p50/p90/p99). |
| Class Distribution | bargauge | Class distribution comparison between production and shadow. |
| Disagreement Rate | stat | Fraction of frames where production and shadow disagree. |
| Latency Comparison | timeseries | Inference latency comparison. |
| Shadow Frames Consumed / sec | stat | Shadow worker consumption rate. |
| Shadow Publish Errors / sec | stat | Shadow worker publish failures. |
| Shadow Worker Up | stat | Shadow worker health status. |
| Debug Trace Link | text | Link to debug traces for detailed frame-level comparison. |

**What to look for:**

- This dashboard is only relevant during a model rollout shadow phase (see [Model Rollout SOP](../runbooks/model-rollout-sop.md)).
- Disagreement rate above 15% is the rollback trigger threshold.
- Shadow latency significantly higher than production may indicate the new model is too large for the current GPU.
- Class distribution shifts may be intentional (improved model) or problematic (regression).

**When to escalate:**

- Disagreement rate above 15% sustained for 1 hour.
- Shadow worker down (`ShadowWorkerDown` alert).
- Shadow publish errors sustained.

---

### 8. Storage Tiering

**UID:** `storage-tiering` | **URL:** `/d/storage-tiering`

<!-- screenshot placeholder: storage-tiering -->

| Panel | Type | What it shows |
|-------|------|---------------|
| MinIO Total Storage | stat | Total storage used across all buckets. |
| Per-Bucket Size | timeseries | Storage consumption per MinIO bucket over time. |
| Tier Distribution | piechart | Breakdown of storage by tier (hot/warm/cold). |
| Object Count by Bucket | bargauge | Number of objects per bucket. |
| Lifecycle Expiration Activity | timeseries | Rate of objects being expired by lifecycle policies. |
| Projected Monthly Cost | stat | Estimated monthly storage cost. |

**What to look for:**

- Per-bucket size should follow predictable growth patterns. Sudden spikes may indicate a pipeline anomaly flooding a bucket.
- Lifecycle expiration activity should show daily patterns. If it drops to zero, lifecycle policies may have stopped running.
- Compare actual tier distribution with the expected mix based on lifecycle policies.

**When to escalate:**

- MinIO disk usage above 80% (`MinIODiskUsageHigh`).
- Lifecycle expiration stalled for >24 hours (`MinIOLifecycleExpirationStalled`).
- Any bucket approaching its capacity warning threshold.

**Runbook:** [Scaling — MinIO](../runbooks/scaling.md)

---

## Alert Rule Reference

### Stream Alerts (`stream-alerts.yml`)

| Alert | Severity | Description |
|-------|----------|-------------|
| `CameraOffline` | warning | Camera unreachable for >5 minutes. |
| `DecodeErrorRateHigh` | warning | High frame decode failure rate. |
| `MotionDutyCycleAnomaly` | warning | Motion duty cycle outside expected range. |
| `NatsPublishLatencyHigh` | warning | Edge-to-NATS publish latency elevated. |
| `EdgeBufferFillHigh` | warning | Edge local buffer approaching capacity. |

**Runbook:** [Incident Response — Stream Alerts](../runbooks/incident-response.md)

---

### Inference Alerts (`inference-alerts.yml`)

| Alert | Severity | Description |
|-------|----------|-------------|
| `InferenceLatencyP99High` | warning | Detection inference p99 latency elevated. |
| `InferenceVramHeadroomLow` | warning | GPU VRAM usage approaching limit. |
| `EmbeddingLatencyP99High` | warning | OSNet embedding inference p99 latency elevated. |
| `InferenceConsumerLagHigh` | warning | Inference worker Kafka consumer lag growing. |
| `InferencePublishErrors` | warning | Inference worker failing to publish results. |

**Runbook:** [Incident Response — Inference Alerts](../runbooks/incident-response.md)

---

### Bus Alerts (`bus-alerts.yml`)

| Alert | Severity | Description |
|-------|----------|-------------|
| `KafkaConsumerLagHigh` | warning | Kafka consumer lag elevated. |
| `KafkaConsumerLagCritical` | critical | Kafka consumer lag dangerously high. |
| `BridgeSpoolFillWarn` | warning | Ingress bridge NVMe spool above threshold. |
| `BridgeSpoolFillCritical` | critical | Ingress bridge spool nearly full. |
| `BridgeSchemaRejectionRate` | warning | Messages failing protobuf schema validation. |
| `BridgeDLQPublishing` | warning | Messages being sent to dead-letter queue. |

**Runbook:** [Incident Response — Bus Alerts](../runbooks/incident-response.md)

---

### Storage Alerts (`storage-alerts.yml`)

| Alert | Severity | Description |
|-------|----------|-------------|
| `TimescaleDBWriteErrors` | critical | Write operations to TimescaleDB are failing. |
| `BulkWriteLatencyHigh` | warning | Bulk collector write latency elevated. |
| `BulkRowsStagedHigh` | warning | Bulk collector staging buffer growing. |
| `DecodeServiceErrorRateHigh` | warning | Decode service producing errors. |
| `DecodePublishErrors` | warning | Decode service failing to publish decoded frames. |

**Runbook:** [Incident Response — Storage Alerts](../runbooks/incident-response.md)

---

### Clock Alerts (`clock-alerts.yml`, `clock-drift.yml`)

| Alert | Severity | Description |
|-------|----------|-------------|
| `ClockDriftCollectorDown` | warning | Clock drift monitoring collector is not running. |
| `BridgeClockDriftDetected` | warning | Ingress bridge detected clock skew between edge and center. |
| `ClockSkewMultiplePairsWarn` | warning | Multiple camera pairs showing clock skew. |
| `ClockSkewWarn` | warning | Clock skew between a camera pair exceeds 500ms. |
| `ClockSkewCritical` | critical | Clock skew exceeds 2000ms. Cross-camera ordering is unreliable. |

**Runbook:** [Incident Response](../runbooks/incident-response.md), [Time Sync Policy](../time-sync-policy.md)

---

### Triton Alerts (`triton-alerts.yml`)

| Alert | Severity | Description |
|-------|----------|-------------|
| `TritonVramWarn` | warning | Triton VRAM usage elevated. |
| `TritonVramCritical` | critical | Triton VRAM near limit. OOM risk. |
| `TritonQueueDelayWarn` | warning | Inference queue delay elevated. |
| `TritonQueueDelayCritical` | critical | Inference queue delay critically high. GPU saturated. |
| `TritonInferenceErrorRate` | warning | Triton returning inference errors. |
| `TritonModelNotReady` | critical | A required model is not loaded in Triton. |

**Runbook:** [Incident Response](../runbooks/incident-response.md), [Service Restart — Triton](../runbooks/service-restart.md)

---

### MTMC Alerts (`mtmc-alerts.yml`)

| Alert | Severity | Description |
|-------|----------|-------------|
| `MtmcMatchRateLow` | warning | Cross-camera match rate below expected baseline. |
| `MtmcCheckpointLagCritical` | critical | FAISS checkpoint write lagging >300s. |
| `MtmcFaissIndexAnomaly` | warning | FAISS index size outside expected bounds. |
| `MtmcRejectRateHigh` | warning | High fraction of embedding comparisons rejected. |
| `MtmcEmbeddingConsumptionStopped` | critical | MTMC service stopped consuming embeddings. |

---

### Storage Tiering Alerts (`storage-tier-alerts.yml`)

| Alert | Severity | Description |
|-------|----------|-------------|
| `MinIOBucketCapacityWarn` | warning | A MinIO bucket approaching capacity threshold. |
| `MinIOBucketCapacityCritical` | critical | A MinIO bucket at critical capacity. |
| `MinIODiskUsageHigh` | warning | MinIO disk usage above 70%. |
| `MinIODiskUsageCritical` | critical | MinIO disk usage above 90%. |
| `MinIOLifecycleExpirationStalled` | warning | MinIO lifecycle expiration has not run recently. |

**Runbook:** [Scaling — MinIO](../runbooks/scaling.md)

---

### Shadow Alerts (`shadow-alerts.yml`)

| Alert | Severity | Description |
|-------|----------|-------------|
| `ShadowDetectionCountDivergence` | warning | Production and shadow detection counts diverging. |
| `ShadowLatencyHigh` | warning | Shadow inference worker latency elevated. |
| `ShadowPublishErrors` | warning | Shadow worker failing to publish results. |
| `ShadowWorkerDown` | critical | Shadow inference worker is unreachable. |

**Runbook:** [Model Rollout SOP — Shadow Phase](../runbooks/model-rollout-sop.md)
