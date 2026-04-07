# Pre-Deployment TODO

Items discovered during development that must be resolved before pilot deployment.
Updated after each task completion. Referenced in PROJECT-STATUS.md.

---

## Kafka Topic Gaps

- [ ] Add `frames.decoded.refs` to `infra/kafka/topics.yaml` — introduced by P1-V03 (decode service publishes here), not yet in canonical topic list
- [ ] Add `bulk.detections` to `infra/kafka/topics.yaml` — introduced by P1-V04/P1-V05 (inference worker publishes, bulk collector consumes), not yet in canonical topic list

## Service Reconfiguration

- [ ] Inference worker (P1-V04) `input_topic` must change from `frames.sampled.refs` to `frames.decoded.refs` when decode service (P1-V03) is deployed — currently both services consume the same topic

## Query API Gaps

- [ ] Track detail endpoint (`GET /tracks/{id}`) returns `thumbnail_url: null` — needs a frame-reference lookup table or stored thumbnail URI in `local_tracks` to resolve

## Debug Trace Pipeline Gaps (P1-V07)

- [ ] Wire `TraceCollector` into inference worker `main.py` pipeline loop — currently `main.py` still uses the old `DebugTracer`, new enrichment methods (raw detections, tracker delta, attributes, model versions) are not called
- [ ] Call `TraceCollector.ensure_bucket()` at inference worker startup to create `debug-traces` bucket with 30-day lifecycle
- [ ] Debug trace query endpoint lists MinIO objects directly (no DB index) — acceptable for pilot but will need a metadata table at scale

## Tracker Bake-Off Gaps (P1-E02)

- [ ] BoT-SORT is not implemented in the repo — only ByteTrack exists in `services/inference-worker/tracker.py`. Need to implement or integrate BoT-SORT before promoting it to production
- [ ] Live tracker bake-off still needed — proxy uses MOT17 private detections, not YOLOv8-L on pilot clips. Re-validate recommendation once `data/eval/mot/` is populated
- [ ] BoT-SORT published throughput (6.8 FPS) is ~4x slower than ByteTrack (29.6 FPS) — measure real latency on target GPU stack before committing to BoT-SORT in production

## Edge Filter Calibration Gaps (P1-E03)

- [ ] Edge agent does not subscribe to `site.{site_id}.control.{camera_id}.calibrate` — calibration harness publishes this command but the agent has no control consumer to disable motion filtering
- [ ] NATS ACL template (`infra/nats/nats-server.conf`) missing control subject authorization — add `site.<site_id>.control.>` to the appropriate user blocks
- [ ] Prometheus node-exporter textfile collector not configured to scrape `artifacts/calibration/prometheus/` — calibration metrics won't appear in Grafana until this path is added to the monitoring stack
