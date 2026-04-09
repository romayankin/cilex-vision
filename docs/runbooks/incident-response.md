---
version: "1.0.0"
status: P2-X02
created_by: codex-cli
date: "2026-04-10"
---

# Incident Response Runbook

**Related documents:** `docs/runbooks/model-rollout-sop.md`, `docs/deployment-guide-pilot.md`, `infra/prometheus/rules/*.yml`, `scripts/deploy/health-check-all.sh`
**Scope:** Operator response to Prometheus alerts across the pilot and multi-node Cilex Vision deployment.

---

## Overview

Use this runbook when Grafana, Prometheus, or Alertmanager reports any of the alerts listed below.

| Step | Action |
|------|--------|
| 1 | Confirm the alert in Grafana and note the affected `camera_id`, `site_id`, `topic`, `partition`, or `model` label. |
| 2 | Run the diagnosis commands exactly as written. Do not restart multiple components at once unless the fix section says to do so. |
| 3 | If the alert is `critical`, or if a `warning` lasts longer than 15 minutes after the fix, escalate. |

### Operator Assumptions

- On the pilot, run the commands on the single host that runs the `pilot-*` containers.
- On multi-node deployments, run the same command on the host from `infra/ansible/inventory/production.yml` that owns the affected container.
- For a quick full-stack check, use:

```bash
scripts/deploy/health-check-all.sh --inventory infra/ansible/inventory/production.yml
```

---

## Stream Alerts

### CameraOffline
**Severity:** warning
**What:** The camera has been unreachable for more than 5 minutes. The RTSP feed is down, credentials are wrong, or the edge agent is not reading the stream.
**Dashboard:** Grafana → Stream Health (`/d/stream-health`)
**Diagnose:**
```bash
docker logs pilot-edge-agent --tail 100 | grep -Ei "camera|rtsp|error|reconnect"
ffprobe -v quiet -rtsp_transport tcp -i "rtsp://USER:PASS@CAMERA_IP/STREAM"
curl -G -fsS http://localhost:9090/api/v1/query \
  --data-urlencode 'query=edge_camera_uptime_ratio'
```
**Fix:**
```bash
docker restart pilot-edge-agent
bash scripts/pilot/list-cameras.sh
```
If the RTSP test still fails, correct the URL in `infra/pilot/cameras.yaml` or replace the failed camera hardware.
**Escalate:** Escalate to site operations if cabling, PoE, switch port, or camera hardware is suspected. Escalate to platform engineering if the RTSP test succeeds but `pilot-edge-agent` still cannot connect.

### DecodeErrorRateHigh
**Severity:** warning
**What:** The edge agent decoder is seeing a high rate of frame decode failures. The stream codec or transport settings may be wrong.
**Dashboard:** Grafana → Stream Health (`/d/stream-health`)
**Diagnose:**
```bash
docker logs pilot-edge-agent --tail 100 | grep -Ei "decode|gstreamer|h264|h265|error"
ffprobe -hide_banner -rtsp_transport tcp "rtsp://USER:PASS@CAMERA_IP/STREAM"
curl -G -fsS http://localhost:9090/api/v1/query \
  --data-urlencode 'query=edge_decode_errors_total'
```
**Fix:**
```bash
docker restart pilot-edge-agent
```
If the camera offers multiple stream profiles, switch to the primary H.264 stream and update `infra/pilot/cameras.yaml`, then restart the edge agent again.
**Escalate:** Escalate to site operations if the camera stream itself is corrupted. Escalate to platform engineering if decode errors continue after switching to a supported stream profile.

### MotionDutyCycleAnomaly
**Severity:** warning
**What:** The edge motion filter is passing almost no frames for the camera. The camera may be pointed at a static scene, blocked, or using thresholds that are too strict.
**Dashboard:** Grafana → Stream Health (`/d/stream-health`)
**Diagnose:**
```bash
curl -G -fsS http://localhost:9090/api/v1/query \
  --data-urlencode 'query=edge_motion_frames_total'
curl -G -fsS http://localhost:9090/api/v1/query \
  --data-urlencode 'query=edge_static_frames_filtered_total'
docker logs pilot-edge-agent --tail 100 | grep -Ei "motion|static|filter"
python3 scripts/calibration/edge_filter_calibration.py --help
```
**Fix:**
```bash
python3 scripts/calibration/edge_filter_calibration.py \
  --camera-id CAM_ID \
  --edge-config infra/pilot/cameras.yaml \
  --window-s 600
docker restart pilot-edge-agent
```
Also confirm the camera is not blocked or pointed at a blank wall.
**Escalate:** Escalate to site operations for physical obstruction or bad camera placement. Escalate to ML or platform engineering if recalibration is needed across many cameras.

### NatsPublishLatencyHigh
**Severity:** warning
**What:** The edge agent can read frames, but publishing them to NATS is slow. This usually means NATS is unhealthy or the network between edge and NATS is degraded.
**Dashboard:** Grafana → Stream Health (`/d/stream-health`)
**Diagnose:**
```bash
curl -fsS http://localhost:8222/healthz
curl -fsS http://localhost:8222/jsz | python3 -m json.tool | head -40
docker logs pilot-edge-agent --tail 100 | grep -Ei "nats|publish|timeout"
```
**Fix:**
```bash
docker restart pilot-nats
sleep 10
docker restart pilot-edge-agent
```
If multi-node, restart the site-local NATS container on the affected edge host, then restart the edge-agent on the same site.
**Escalate:** Escalate to network operations if latency affects one site only. Escalate to platform engineering if NATS health stays bad after restart.

### EdgeBufferFillHigh
**Severity:** warning
**What:** The edge agent is buffering too much local data because it cannot publish upstream fast enough.
**Dashboard:** Grafana → Stream Health (`/d/stream-health`)
**Diagnose:**
```bash
curl -G -fsS http://localhost:9090/api/v1/query \
  --data-urlencode 'query=edge_buffer_fill_bytes'
docker logs pilot-edge-agent --tail 100 | grep -Ei "buffer|publish|nats"
curl -fsS http://localhost:8222/healthz
```
**Fix:**
```bash
docker restart pilot-nats
docker restart pilot-edge-agent
```
If the buffer keeps rising, disable the noisiest camera temporarily in `infra/pilot/cameras.yaml`, then restart `pilot-edge-agent`.
**Escalate:** Escalate immediately if the buffer exceeds 80% of local disk budget or continues to grow after the NATS restart. Site operations own disk-full conditions; platform engineering owns NATS or edge-agent faults.

---

## Bus Alerts

### KafkaConsumerLagHigh
**Severity:** warning
**What:** A Kafka consumer is falling behind, but data loss is not yet imminent.
**Dashboard:** Grafana → Bus Health (`/d/bus-health`)
**Diagnose:**
```bash
docker exec pilot-kafka /opt/bitnami/kafka/bin/kafka-consumer-groups.sh \
  --bootstrap-server localhost:9092 \
  --all-groups --describe
docker logs pilot-bulk-collector --tail 100 | grep -Ei "lag|error|retry|commit"
```
**Fix:**
```bash
docker restart pilot-bulk-collector
```
If inference is overproducing, lower ingest pressure by reducing `DECODE__SAMPLER__TARGET_FPS`, then restart `pilot-decode-service`.
**Escalate:** Escalate to platform engineering if lag remains above 10,000 for 15 minutes or if multiple consumer groups are lagging at once.

### KafkaConsumerLagCritical
**Severity:** critical
**What:** Kafka lag is high enough that downstream consumers may miss SLAs or risk message loss if disks fill.
**Dashboard:** Grafana → Bus Health (`/d/bus-health`)
**Diagnose:**
```bash
docker exec pilot-kafka /opt/bitnami/kafka/bin/kafka-consumer-groups.sh \
  --bootstrap-server localhost:9092 \
  --all-groups --describe
docker stats --no-stream pilot-kafka pilot-bulk-collector pilot-inference-worker
```
**Fix:**
```bash
docker restart pilot-bulk-collector
docker restart pilot-kafka
```
After restart, re-check lag every 2 minutes until it begins to fall.
**Escalate:** Escalate immediately to platform engineering. If the backlog continues to rise after restart, open a severity-1 incident.

### BridgeSpoolFillWarn
**Severity:** warning
**What:** The ingress bridge disk spool is filling because Kafka is slow or unreachable.
**Dashboard:** Grafana → Bus Health (`/d/bus-health`)
**Diagnose:**
```bash
docker logs pilot-ingress-bridge --tail 100 | grep -Ei "spool|kafka|retry|backoff"
docker exec pilot-kafka /opt/bitnami/kafka/bin/kafka-topics.sh --bootstrap-server localhost:9092 --list
curl -G -fsS http://localhost:9090/api/v1/query \
  --data-urlencode 'query=bridge_spool_fill_pct'
```
**Fix:**
```bash
docker restart pilot-kafka
sleep 10
docker restart pilot-ingress-bridge
```
If the spool does not drain, reduce upstream load by temporarily disabling one or more cameras.
**Escalate:** Escalate to platform engineering if spool stays above 80% for more than 10 minutes.

### BridgeSpoolFillCritical
**Severity:** critical
**What:** The ingress bridge spool is almost full. If not cleared quickly, incoming messages will be dropped.
**Dashboard:** Grafana → Bus Health (`/d/bus-health`)
**Diagnose:**
```bash
curl -G -fsS http://localhost:9090/api/v1/query \
  --data-urlencode 'query=bridge_spool_fill_pct'
docker logs pilot-ingress-bridge --tail 100 | grep -Ei "spool|disk|full|drop"
df -h
```
**Fix:**
```bash
docker restart pilot-kafka
docker restart pilot-ingress-bridge
```
If necessary, stop the edge-agent temporarily to stop new intake:
```bash
docker stop pilot-edge-agent
```
Restart `pilot-edge-agent` only after `bridge_spool_fill_pct` drops below 50%.
**Escalate:** Escalate immediately to platform engineering and site operations if free disk is below 15%.

### BridgeSchemaRejectionRate
**Severity:** warning
**What:** The ingress bridge is rejecting messages because they do not match the expected protobuf schema.
**Dashboard:** Grafana → Bus Health (`/d/bus-health`)
**Diagnose:**
```bash
docker logs pilot-ingress-bridge --tail 100 | grep -Ei "schema|protobuf|reject"
docker logs pilot-edge-agent --tail 100 | grep -Ei "proto|publish|schema"
```
**Fix:**
```bash
docker restart pilot-edge-agent
docker restart pilot-ingress-bridge
```
If the alert returns immediately after both restarts, stop the rollout and keep the site on the last known-good image set.
**Escalate:** Escalate to platform engineering. This usually means a version mismatch that operators should not patch manually.

### BridgeDLQPublishing
**Severity:** warning
**What:** The ingress bridge is sending messages to its dead-letter queue because it cannot publish them normally.
**Dashboard:** Grafana → Bus Health (`/d/bus-health`)
**Diagnose:**
```bash
docker logs pilot-ingress-bridge --tail 100 | grep -Ei "dlq|dead-letter|publish"
docker exec pilot-kafka /opt/bitnami/kafka/bin/kafka-topics.sh --bootstrap-server localhost:9092 --list | grep dlq
```
**Fix:**
```bash
docker restart pilot-ingress-bridge
docker restart pilot-kafka
```
If DLQ traffic continues, keep the DLQ topic intact for later analysis. Do not delete it.
**Escalate:** Escalate to platform engineering if DLQ rate stays above zero for 10 minutes.

---

## Clock Alerts

### ClockDriftCollectorDown
**Severity:** warning
**What:** The clock drift collector stopped producing `clock_skew_ms`, so clock health is currently unknown.
**Dashboard:** Grafana → Stream Health (`/d/stream-health`)
**Diagnose:**
```bash
docker logs node-exporter --tail 50
crontab -l | grep "cilex clock drift collector"
ls -l /var/lib/node_exporter/textfile_collector/clock_skew.prom
```
**Fix:**
```bash
systemctl restart cron
docker restart node-exporter
/usr/bin/python3 /opt/cilex/bin/clock_drift_check.py \
  --targets /opt/cilex/monitoring/promtail/clock-targets.json \
  --output /var/lib/node_exporter/textfile_collector/clock_skew.prom
```
**Escalate:** Escalate to platform engineering if the metrics file is not recreated within 5 minutes.

### BridgeClockDriftDetected
**Severity:** warning
**What:** The ingress bridge is receiving messages whose core ingest time appears earlier than the edge receive time. This indicates clock drift between hosts.
**Dashboard:** Grafana → Bus Health (`/d/bus-health`)
**Diagnose:**
```bash
docker logs pilot-ingress-bridge --tail 100 | grep -Ei "clock|drift|timestamp"
chronyc tracking
chronyc sources -v
```
**Fix:**
```bash
sudo systemctl restart chrony
docker restart pilot-ingress-bridge
```
Run the same `chronyc` commands on the affected edge host if the deployment is multi-node.
**Escalate:** Escalate to platform engineering if drift persists after Chrony restart. Escalate to infrastructure or NTP administrators if multiple hosts show bad sources.

### ClockSkewMultiplePairsWarn
**Severity:** warning
**What:** More than two camera pairs exceed the 500 ms skew threshold. This is usually a site-wide or cluster-wide time sync issue.
**Dashboard:** Grafana → Stream Health (`/d/stream-health`)
**Diagnose:**
```bash
cat /var/lib/node_exporter/textfile_collector/clock_skew.prom
chronyc tracking
chronyc sources -v
```
**Fix:**
```bash
sudo systemctl restart chrony
docker restart node-exporter
```
If this is a multi-node deployment, run the same Chrony check on the monitoring host and the edge hosts in the affected site.
**Escalate:** Escalate to infrastructure operations if more than one host reports bad or unreachable NTP sources.

### ClockSkewWarn
**Severity:** warning
**What:** One camera pair exceeds 500 ms skew. Correlation across cameras may become unreliable.
**Dashboard:** Grafana → Stream Health (`/d/stream-health`)
**Diagnose:**
```bash
grep clock_skew_ms /var/lib/node_exporter/textfile_collector/clock_skew.prom
chronyc tracking
```
**Fix:**
```bash
sudo systemctl restart chrony
```
Re-check the metric after 2 minutes.
**Escalate:** Escalate to platform engineering if the same pair remains above 500 ms for more than 15 minutes.

### ClockSkewCritical
**Severity:** critical
**What:** A camera pair exceeds 2,000 ms skew. Cross-camera ordering is no longer trustworthy.
**Dashboard:** Grafana → Stream Health (`/d/stream-health`)
**Diagnose:**
```bash
grep clock_skew_ms /var/lib/node_exporter/textfile_collector/clock_skew.prom
chronyc tracking
chronyc sources -v
```
**Fix:**
```bash
sudo systemctl restart chrony
docker restart pilot-edge-agent
```
If the camera pair belongs to different hosts, restart Chrony on both hosts before restarting the edge agent.
**Escalate:** Escalate immediately to infrastructure operations and platform engineering.

---

## Inference Alerts

### InferenceLatencyP99High
**Severity:** warning
**What:** End-to-end detection inference latency is too high. The inference worker or Triton is overloaded.
**Dashboard:** Grafana → Inference Performance (`/d/inference-perf`)
**Diagnose:**
```bash
curl -fsS http://localhost:8002/metrics | grep -E "nv_inference_queue_duration_us|nv_gpu_memory"
docker logs pilot-inference-worker --tail 100 | grep -Ei "latency|timeout|triton|infer"
docker stats --no-stream pilot-inference-worker pilot-triton
```
**Fix:**
```bash
docker restart pilot-triton
sleep 15
docker restart pilot-inference-worker
```
If the site is in shadow mode, unload the candidate model version before restarting.
**Escalate:** Escalate to ML or platform engineering if p99 stays above 200 ms for 15 minutes.

### InferenceVramHeadroomLow
**Severity:** warning
**What:** GPU VRAM headroom is below 15%. Additional workload may trigger out-of-memory errors.
**Dashboard:** Grafana → Inference Performance (`/d/inference-perf`)
**Diagnose:**
```bash
nvidia-smi
curl -fsS http://localhost:8002/metrics | grep nv_gpu_memory_used_bytes
```
**Fix:**
```bash
curl -s -X POST http://localhost:8000/v2/repository/models/yolov8l/unload
curl -s -X POST http://localhost:8000/v2/repository/models/osnet/unload
```
Reload only the required production models after confirming available VRAM. If shadow deployment is active, unload the shadow model first.
**Escalate:** Escalate to ML engineering before unloading a production model. Escalate immediately if VRAM remains above 85% after removing shadow load.

### EmbeddingLatencyP99High
**Severity:** warning
**What:** The Re-ID embedding path is too slow. The OSNet model or GPU is saturated.
**Dashboard:** Grafana → Inference Performance (`/d/inference-perf`)
**Diagnose:**
```bash
curl -fsS http://localhost:8002/metrics | grep -E "osnet|nv_inference_queue_duration_us"
docker logs pilot-inference-worker --tail 100 | grep -Ei "embedding|osnet|timeout"
```
**Fix:**
```bash
docker restart pilot-triton
sleep 15
docker restart pilot-inference-worker
```
If MTMC is not required during the incident window, temporarily disable embedding consumers before restarting.
**Escalate:** Escalate to ML engineering if the alert returns immediately after restart.

### InferenceConsumerLagHigh
**Severity:** warning
**What:** The inference worker is not consuming decoded frames fast enough.
**Dashboard:** Grafana → Inference Performance (`/d/inference-perf`)
**Diagnose:**
```bash
docker exec pilot-kafka /opt/bitnami/kafka/bin/kafka-consumer-groups.sh \
  --bootstrap-server localhost:9092 \
  --describe --group detector-worker
docker logs pilot-inference-worker --tail 100 | grep -Ei "lag|poll|rebalance|timeout"
```
**Fix:**
```bash
docker restart pilot-inference-worker
```
If lag keeps rising, lower `DECODE__SAMPLER__TARGET_FPS` and restart `pilot-decode-service`.
**Escalate:** Escalate to platform engineering if lag stays above 5,000 for 15 minutes.

### InferencePublishErrors
**Severity:** warning
**What:** The inference worker is producing detections, tracklets, or embeddings but cannot publish them reliably to Kafka.
**Dashboard:** Grafana → Inference Performance (`/d/inference-perf`)
**Diagnose:**
```bash
docker logs pilot-inference-worker --tail 100 | grep -Ei "publish|kafka|error"
docker exec pilot-kafka /opt/bitnami/kafka/bin/kafka-topics.sh --bootstrap-server localhost:9092 --list
```
**Fix:**
```bash
docker restart pilot-kafka
sleep 10
docker restart pilot-inference-worker
```
**Escalate:** Escalate to platform engineering if publish errors continue for more than 5 minutes after restart.

---

## Storage Alerts

### TimescaleDBWriteErrors
**Severity:** critical
**What:** The bulk collector cannot write to TimescaleDB. Metadata persistence is failing.
**Dashboard:** Grafana → Storage (`/d/storage`)
**Diagnose:**
```bash
docker logs pilot-bulk-collector --tail 100 | grep -Ei "write|copy|postgres|error"
docker exec pilot-timescaledb pg_isready -U cilex -d vidanalytics
df -h
```
**Fix:**
```bash
docker restart pilot-timescaledb
sleep 15
docker restart pilot-bulk-collector
```
If disk is full, free space or extend the volume before restarting the collector again.
**Escalate:** Escalate immediately to database or platform engineering.

### BulkWriteLatencyHigh
**Severity:** warning
**What:** TimescaleDB writes are succeeding, but COPY latency is too high.
**Dashboard:** Grafana → Storage (`/d/storage`)
**Diagnose:**
```bash
docker logs pilot-bulk-collector --tail 100 | grep -Ei "latency|flush|copy"
docker exec pilot-timescaledb psql -U cilex -d vidanalytics -c "SELECT now();"
```
**Fix:**
```bash
docker restart pilot-bulk-collector
```
If latency stays high, reduce ingest volume by lowering sampled FPS or disabling non-critical cameras temporarily.
**Escalate:** Escalate to database engineering if write latency remains above 1 second for 15 minutes.

### BulkRowsStagedHigh
**Severity:** warning
**What:** The bulk collector staging buffer is growing. The collector is not flushing fast enough.
**Dashboard:** Grafana → Storage (`/d/storage`)
**Diagnose:**
```bash
curl -G -fsS http://localhost:9090/api/v1/query \
  --data-urlencode 'query=bulk_rows_staged'
docker logs pilot-bulk-collector --tail 100 | grep -Ei "staged|flush|batch"
```
**Fix:**
```bash
docker restart pilot-bulk-collector
```
If it grows again, lower sampled FPS on the decode service and restart `pilot-decode-service`.
**Escalate:** Escalate to platform engineering if the staged row count stays above 10,000 for 15 minutes.

### DecodeServiceErrorRateHigh
**Severity:** warning
**What:** The central decode service is failing to decode incoming frames at an elevated rate.
**Dashboard:** Grafana → Storage (`/d/storage`)
**Diagnose:**
```bash
docker logs pilot-decode-service --tail 100 | grep -Ei "decode|codec|error"
curl -G -fsS http://localhost:9090/api/v1/query \
  --data-urlencode 'query=decode_errors_total'
```
**Fix:**
```bash
docker restart pilot-decode-service
```
If errors are isolated to one camera, disable that camera temporarily and restore the service.
**Escalate:** Escalate to platform engineering if errors continue after restart or affect multiple codecs.

### DecodePublishErrors
**Severity:** warning
**What:** The decode service cannot publish decoded frame references to Kafka.
**Dashboard:** Grafana → Storage (`/d/storage`)
**Diagnose:**
```bash
docker logs pilot-decode-service --tail 100 | grep -Ei "publish|kafka|error"
docker exec pilot-kafka /opt/bitnami/kafka/bin/kafka-topics.sh --bootstrap-server localhost:9092 --list
```
**Fix:**
```bash
docker restart pilot-kafka
sleep 10
docker restart pilot-decode-service
```
**Escalate:** Escalate to platform engineering if the decoded frame topic stays unavailable or errors return immediately.

---

## Triton Alerts

### TritonVramWarn
**Severity:** warning
**What:** GPU VRAM usage is above 85%. The server is close to running out of memory.
**Dashboard:** Grafana → Inference Performance (`/d/inference-perf`)
**Diagnose:**
```bash
nvidia-smi
curl -fsS http://localhost:8002/metrics | grep nv_gpu_memory_used_bytes
```
**Fix:**
```bash
curl -s -X POST http://localhost:8000/v2/repository/models/osnet_reid/unload
```
Unload any non-essential or shadow models first, then re-check `nvidia-smi`.
**Escalate:** Escalate to ML engineering before unloading a production model. Escalate to platform engineering if VRAM usage keeps rising.

### TritonVramCritical
**Severity:** critical
**What:** GPU VRAM usage is above 95%. Triton may fail requests or crash.
**Dashboard:** Grafana → Inference Performance (`/d/inference-perf`)
**Diagnose:**
```bash
nvidia-smi
curl -fsS http://localhost:8000/v2/health/ready
```
**Fix:**
```bash
curl -s -X POST http://localhost:8000/v2/repository/models/yolov8l/unload
docker restart pilot-triton
```
Reload only the approved production models after the server becomes healthy.
**Escalate:** Escalate immediately to ML and platform engineering.

### TritonQueueDelayWarn
**Severity:** warning
**What:** Triton is queuing inference requests for too long. Throughput is no longer matching demand.
**Dashboard:** Grafana → Inference Performance (`/d/inference-perf`)
**Diagnose:**
```bash
curl -fsS http://localhost:8002/metrics | grep nv_inference_queue_duration_us
docker stats --no-stream pilot-triton
```
**Fix:**
```bash
docker restart pilot-triton
sleep 15
docker restart pilot-inference-worker
```
**Escalate:** Escalate to platform engineering if the queue delay stays above 100 ms for 15 minutes.

### TritonQueueDelayCritical
**Severity:** critical
**What:** Triton is severely backlogged. Real-time inference is no longer being met.
**Dashboard:** Grafana → Inference Performance (`/d/inference-perf`)
**Diagnose:**
```bash
curl -fsS http://localhost:8002/metrics | grep nv_inference_queue_duration_us
docker logs pilot-triton --tail 100
```
**Fix:**
```bash
docker restart pilot-triton
docker restart pilot-inference-worker
```
If needed, stop the edge agent for 2-5 minutes to let the system drain.
**Escalate:** Escalate immediately to platform engineering.

### TritonInferenceErrorRate
**Severity:** warning
**What:** Triton is failing more than 1% of inference requests.
**Dashboard:** Grafana → Inference Performance (`/d/inference-perf`)
**Diagnose:**
```bash
docker logs pilot-triton --tail 100 | grep -Ei "error|fail|model"
curl -fsS http://localhost:8000/v2/health/ready
```
**Fix:**
```bash
docker restart pilot-triton
sleep 15
docker restart pilot-inference-worker
```
If a recently loaded model is the cause, unload it and return to the previous version.
**Escalate:** Escalate to ML engineering if failures are tied to one model version. Escalate to platform engineering if Triton itself is unstable.

### TritonModelNotReady
**Severity:** critical
**What:** A model is not serving requests. In EXPLICIT mode this often means the model is not loaded.
**Dashboard:** Grafana → Inference Performance (`/d/inference-perf`)
**Diagnose:**
```bash
curl -fsS http://localhost:8000/v2/health/ready
curl -fsS http://localhost:8000/v2/models/yolov8l
docker logs pilot-triton --tail 100
```
**Fix:**
```bash
curl -s -X POST http://localhost:8000/v2/repository/models/yolov8l/load
curl -s -X POST http://localhost:8000/v2/repository/models/osnet/load
curl -s -X POST http://localhost:8000/v2/repository/models/color_classifier/load
curl -s -X POST http://localhost:8000/v2/repository/models/osnet_reid/load
```
Load only the models that belong on that Triton node. Then restart `pilot-inference-worker` if requests do not resume automatically.
**Escalate:** Escalate immediately to ML engineering if the model still does not enter a ready state.

---

## MTMC Alerts

### MtmcMatchRateLow
**Severity:** warning
**What:** The MTMC service is consuming embeddings but is not producing any matches. This may be a topology, attribute, or model-version issue.
**Dashboard:** Grafana → MTMC Re-ID Health (`/d/mtmc-health`)
**Diagnose:**
```bash
curl -G -fsS http://localhost:9090/api/v1/query \
  --data-urlencode 'query=mtmc_matches_total'
curl -G -fsS http://localhost:9090/api/v1/query \
  --data-urlencode 'query=mtmc_rejects_total'
docker logs mtmc-service --tail 100 | grep -Ei "match|reject|topology|version"
```
**Fix:**
```bash
docker restart mtmc-service
```
Then verify the site topology:
```bash
curl -fsS http://QUERY_API_HOST:8000/topology/SITE_UUID
```
**Escalate:** Escalate to ML engineering if no matches return within 15 minutes after restart.

### MtmcCheckpointLagCritical
**Severity:** critical
**What:** The MTMC service has not written a checkpoint for more than 10 minutes. A restart would risk losing recent index state.
**Dashboard:** Grafana → MTMC Re-ID Health (`/d/mtmc-health`)
**Diagnose:**
```bash
curl -G -fsS http://localhost:9090/api/v1/query \
  --data-urlencode 'query=mtmc_checkpoint_lag_seconds'
docker logs mtmc-service --tail 100 | grep -Ei "checkpoint|minio|save|restore"
ls -l /var/lib/cilex/mtmc-service/checkpoint
```
**Fix:**
```bash
docker restart mtmc-service
```
If local checkpoint files are missing, restore the latest checkpoint from backup before restarting again.
**Escalate:** Escalate immediately to platform engineering because a second failure can lose cross-camera match state.

### MtmcFaissIndexAnomaly
**Severity:** warning
**What:** The FAISS index grew or shrank far more than expected in the last hour.
**Dashboard:** Grafana → MTMC Re-ID Health (`/d/mtmc-health`)
**Diagnose:**
```bash
curl -G -fsS http://localhost:9090/api/v1/query \
  --data-urlencode 'query=mtmc_faiss_index_size'
docker logs mtmc-service --tail 100 | grep -Ei "cleanup|checkpoint|remove|restore"
```
**Fix:**
```bash
docker restart mtmc-service
```
After restart, verify the index size stabilizes and that embeddings are being consumed again.
**Escalate:** Escalate to ML engineering if the index drops sharply after a model rollout or if it grows without bound.

### MtmcRejectRateHigh
**Severity:** warning
**What:** MTMC is rejecting more than 95% of candidates. This usually means topology, attribute data, or model compatibility has drifted.
**Dashboard:** Grafana → MTMC Re-ID Health (`/d/mtmc-health`)
**Diagnose:**
```bash
curl -G -fsS http://localhost:9090/api/v1/query \
  --data-urlencode 'query=mtmc_rejects_total'
docker logs mtmc-service --tail 100 | grep -Ei "reject|threshold|topology|attribute"
```
**Fix:**
```bash
docker restart mtmc-service
```
Then verify that the relevant site topology edges exist and are enabled.
**Escalate:** Escalate to ML engineering if the reject ratio remains above 95% for 15 minutes.

### MtmcEmbeddingConsumptionStopped
**Severity:** critical
**What:** The MTMC service is no longer consuming embeddings. Cross-camera association is effectively offline.
**Dashboard:** Grafana → MTMC Re-ID Health (`/d/mtmc-health`)
**Diagnose:**
```bash
curl -G -fsS http://localhost:9090/api/v1/query \
  --data-urlencode 'query=mtmc_embeddings_consumed_total'
docker logs mtmc-service --tail 100 | grep -Ei "kafka|poll|rebalance|embedding"
docker exec pilot-kafka /opt/bitnami/kafka/bin/kafka-consumer-groups.sh \
  --bootstrap-server localhost:9092 \
  --describe --group mtmc-service
```
**Fix:**
```bash
docker restart mtmc-service
```
If the Kafka topic is unavailable, restart Kafka before restarting MTMC again.
**Escalate:** Escalate immediately to platform engineering and ML engineering.

---

## Escalation Summary

| Situation | Escalate to |
|-----------|-------------|
| Camera hardware, PoE, switch port, cabling, VLAN, RTSP reachability | Site operations |
| Broker, database, object storage, Docker host, Chrony, disk, or networking failures | Platform engineering / infrastructure operations |
| Triton model load issues, shadow rollback, VRAM pressure caused by model rollout, MTMC quality regressions | ML engineering |
| Any `critical` alert not improving within 5 minutes after the listed fix | Incident commander or on-call lead |
