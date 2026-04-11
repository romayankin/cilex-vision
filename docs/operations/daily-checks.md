# Daily Checks

Morning checklist for platform operators. Complete these checks in order at the start of each shift. Each item links to the relevant Grafana dashboard and lists what to look for and when to escalate.

---

## 1. Camera Health

**Dashboard:** Stream Health (`/d/stream-health`)

**Check:**

- [ ] **Camera Uptime %** panel shows all cameras above 99%. Any camera below 95% for more than 5 minutes needs attention.
- [ ] **Active Cameras** count matches the expected number of deployed cameras.
- [ ] **Decode Errors / min** panel is near zero. A sustained rate above 1/min indicates a codec or transport problem.
- [ ] **Edge Buffer Fill** gauge is below 50%. Above 80% means the edge agent cannot forward frames fast enough.
- [ ] **NATS Publish Latency p95** is below 50ms. Spikes above 200ms indicate edge-to-center network issues.

**Escalate if:**

- Any camera has been offline for more than 15 minutes (check `CameraOffline` alert history).
- Edge buffer fill is above 80% and rising.
- Decode error rate is sustained above 5/min.

**Runbook:** [Incident Response — Stream Alerts](../runbooks/incident-response.md)

---

## 2. Pipeline Throughput

**Dashboard:** Inference Performance (`/d/inference-performance`)

**Check:**

- [ ] **Frames Consumed / sec** is stable and consistent with the camera count (expect ~5-15 FPS per camera after motion filtering).
- [ ] **Detections / sec by Class** shows expected class distribution. A sudden drop in any class may indicate a model issue.
- [ ] **Detection Inference Latency** p99 is below 100ms. Sustained latency above 200ms degrades tracking.
- [ ] **Consumer Lag (Inference Worker)** is near zero. Lag above 1000 means the worker cannot keep up.
- [ ] **GPU VRAM Usage %** is below 80%. Above 90% risks OOM failures.
- [ ] **Triton Queue Delay** is below 10ms. Sustained delay above 50ms means the GPU is saturated.

**Escalate if:**

- Consumer lag is growing continuously for more than 10 minutes.
- VRAM usage is above 90%.
- No detections are being produced (pipeline stall).

**Runbook:** [Incident Response — Inference Alerts](../runbooks/incident-response.md)

---

## 3. Storage Health

**Dashboard:** Storage (`/d/storage`)

**Check:**

- [ ] **Rows Written / sec by Table** shows steady write throughput for `detections` and `track_observations`.
- [ ] **Write Latency p99** is below 50ms. Spikes above 200ms indicate TimescaleDB pressure.
- [ ] **Write Errors / min** is zero. Any write errors need immediate investigation.
- [ ] **Rows Currently Staged** is below 10,000. A growing backlog means the bulk collector is falling behind.
- [ ] **Duplicates Skipped / min** is low. A sudden spike may indicate Kafka consumer rebalancing.

**Escalate if:**

- Write errors are non-zero for more than 5 minutes (`TimescaleDBWriteErrors` alert).
- Staged rows are growing continuously above 50,000.
- Write latency p99 exceeds 500ms sustained.

**Runbook:** [Incident Response — Storage Alerts](../runbooks/incident-response.md)

---

## 4. Alert Review

**Tool:** Prometheus / Alertmanager (`http://<prometheus-host>:9090/alerts`)

**Check:**

- [ ] Review all currently firing alerts. Note the `service` label and severity.
- [ ] Check for any `critical` alerts that may have fired and auto-resolved overnight.
- [ ] Verify no `warning` alerts have been firing for more than 1 hour without action.

**Alert groups to review:**

| Group | File | Alert count |
|-------|------|-------------|
| Stream | `stream-alerts.yml` | 5 |
| Inference | `inference-alerts.yml` | 5 |
| Bus | `bus-alerts.yml` | 6 |
| Storage | `storage-alerts.yml` | 5 |
| Clock | `clock-alerts.yml` + `clock-drift.yml` | 5 |
| Triton | `triton-alerts.yml` | 6 |
| MTMC | `mtmc-alerts.yml` | 5 |
| Storage Tiering | `storage-tier-alerts.yml` | 5 |
| Shadow | `shadow-alerts.yml` | 4 |

**Escalate if:**

- Any `critical` alert is firing.
- Any `warning` alert has been active for more than 15 minutes after the documented fix.

**Runbook:** [Incident Response](../runbooks/incident-response.md)

---

## 5. MTMC Health

**Dashboard:** MTMC Re-ID Health (`/d/mtmc-health`)

**Check:**

- [ ] **Match Rate** is above the expected baseline for the site (typically 10-30% of embedding comparisons result in a match).
- [ ] **Reject Rate** is not rising. A sudden increase may indicate a model or threshold issue.
- [ ] **FAISS Index Size** is within expected range. Abnormal growth suggests stale embeddings are not being evicted.
- [ ] **Checkpoint Lag** is below 60 seconds. Lag above 300s means checkpoint writes are failing or stalled.
- [ ] **Embeddings Consumed** rate matches the inference worker output rate.

**Escalate if:**

- Match rate drops to zero (pipeline broken).
- Checkpoint lag exceeds 300 seconds (`MtmcCheckpointLagCritical` alert).
- FAISS index size exceeds expected maximum (check `MtmcFaissIndexAnomaly` alert).

---

## 6. Model Quality

**Dashboard:** Model Quality (`/d/model-quality`)

**Check:**

- [ ] **Detection Count per Class / hour** is stable. Compare with the previous day for anomalies.
- [ ] **Detection Rate by Camera / min** is consistent across cameras. A camera producing significantly fewer detections may have a field-of-view obstruction or exposure issue.
- [ ] **Track Turnover Ratio** is stable. A sharp increase suggests frequent ID switches (tracker instability).
- [ ] **Publish Errors by Topic** is zero across all topics.

**Escalate if:**

- Any class shows zero detections for more than 30 minutes during expected activity hours.
- Track turnover ratio doubles compared to the 7-day average.

---

## 7. Calibration Status

**Tool:** Check the latest calibration report in `artifacts/calibration/`

**Check:**

- [ ] Automated calibration ran successfully at the scheduled time (02:00 UTC by default).
- [ ] No cameras were skipped due to being offline during the calibration window.
- [ ] Calibration metrics are within acceptable ranges (check Prometheus textfile metrics for `calibration_*`).

**Escalate if:**

- Calibration has not run for more than 48 hours.
- A camera consistently fails calibration across multiple runs.

---

## 8. Drift Monitoring

**Tool:** Check the latest drift report in `artifacts/monitoring/`

**Check:**

- [ ] Hourly drift detector ran successfully (check cron logs or systemd journal).
- [ ] No camera/class groups are flagged as drifted (KS p-value < 0.01 or KL divergence > 0.5).
- [ ] Check Prometheus textfile metrics for `confidence_drift_flag` — all values should be 0.

**Escalate if:**

- Multiple camera/class groups show drift simultaneously (may indicate a model issue rather than an environmental change).
- Drift persists for more than 24 hours on the same camera/class combination.
- The baseline is more than 30 days old and needs to be refreshed (see [Maintenance Windows](maintenance-windows.md)).

---

## Quick Health Check Command

For a fast programmatic check of all services:

```bash
scripts/deploy/health-check-all.sh --inventory infra/ansible/inventory/production.yml
```

This verifies all services are reachable and responding to health endpoints.
