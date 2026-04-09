# Pre-Deployment TODO

Items discovered during development that must be resolved before pilot deployment.
Updated after each task completion. Referenced in PROJECT-STATUS.md.

---

## Kafka Topic Gaps

- [x] Add `frames.decoded.refs` to `infra/kafka/topics.yaml` — introduced by P1-V03 (decode service publishes here), not yet in canonical topic list
- [x] Add `bulk.detections` to `infra/kafka/topics.yaml` — introduced by P1-V04/P1-V05 (inference worker publishes, bulk collector consumes), not yet in canonical topic list

## Service Reconfiguration

- [x] Inference worker (P1-V04) `input_topic` must change from `frames.sampled.refs` to `frames.decoded.refs` when decode service (P1-V03) is deployed — currently both services consume the same topic

## Query API Gaps

- [ ] Track detail endpoint (`GET /tracks/{id}`) returns `thumbnail_url: null` — needs a frame-reference lookup table or stored thumbnail URI in `local_tracks` to resolve
- [ ] Events endpoint does not expose a signed thumbnail URL — `P2-V04` stores `thumbnail_uri` in `events.metadata_jsonb`, but `services/query-api/routers/events.py` only signs `clip_uri`

## Debug Trace Pipeline Gaps (P1-V07)

- [x] Wire `TraceCollector` into inference worker `main.py` pipeline loop — currently `main.py` still uses the old `DebugTracer`, new enrichment methods (raw detections, tracker delta, attributes, model versions) are not called
- [x] Call `TraceCollector.ensure_bucket()` at inference worker startup to create `debug-traces` bucket with 30-day lifecycle
- [ ] Debug trace query endpoint lists MinIO objects directly (no DB index) — acceptable for pilot but will need a metadata table at scale

## Tracker Bake-Off Gaps (P1-E02)

- [ ] BoT-SORT is not implemented in the repo — only ByteTrack exists in `services/inference-worker/tracker.py`. Need to implement or integrate BoT-SORT before promoting it to production
- [ ] Live tracker bake-off still needed — proxy uses MOT17 private detections, not YOLOv8-L on pilot clips. Re-validate recommendation once `data/eval/mot/` is populated
- [ ] BoT-SORT published throughput (6.8 FPS) is ~4x slower than ByteTrack (29.6 FPS) — measure real latency on target GPU stack before committing to BoT-SORT in production

## Edge Filter Calibration Gaps (P1-E03)

- [ ] Edge agent does not subscribe to `site.{site_id}.control.{camera_id}.calibrate` — calibration harness publishes this command but the agent has no control consumer to disable motion filtering
- [ ] NATS ACL template (`infra/nats/nats-server.conf`) missing control subject authorization — add `site.<site_id>.control.>` to the appropriate user blocks
- [ ] Prometheus node-exporter textfile collector not configured to scrape `artifacts/calibration/prometheus/` — calibration metrics won't appear in Grafana until this path is added to the monitoring stack

## Topology Service Gaps (P0-D05)

- [x] Topology FastAPI router (`services/topology/api.py`) not registered in any running app — wire into `services/query-api/main.py` or deploy as a standalone service
- [ ] Run `seed.py --apply` against the pilot DB during infrastructure setup to populate the demo 4-camera topology
- [ ] `zone_id` is stored in `cameras.config_json` JSONB — document this convention for the MTMC service (Phase 2) which needs zone-aware matching

## Clip Pipeline Gaps (P2-V04)

- [ ] Decoded frame keys are `camera_id/YYYY-MM-DD/frame_id.jpg`, so clip extraction currently filters source frames by MinIO `last_modified` instead of a true capture-time index. Add a decoded-frame metadata table or timestamped object naming scheme before relying on frame-accurate clips
- [ ] `archive.transcode.completed` is contract-bound to `vidanalytics.v1.frame.FrameRef`, but `P2-V04` uses it to announce event clip completion. Consider a dedicated clip-oriented topic/schema if downstream consumers need stronger semantics

## MTMC Infrastructure Gaps (P2-O01)

- [ ] `services/mtmc-service/config.py` only supports `kafka_security_protocol`; it has no SASL/SCRAM or TLS file settings, so the new MTMC infra playbook defaults to `PLAINTEXT` and cannot yet connect to the secured Kafka deployment from `deploy-kafka.yml`
- [ ] `infra/ansible/playbooks/deploy-services.yml` builds from each service directory only, but repo service Dockerfiles such as `services/mtmc-service/Dockerfile` expect a repo-root build context (`proto/` plus `services/...`). Generalize the shared service playbook before relying on it for newer services

## Model Rollout SOP Gaps (P0-D09)

- [ ] No automated rollout orchestration — SOP is manual copy-paste commands. Consider an Ansible playbook or rollout script to reduce human error during model cutover
- [ ] Inference worker has no in-process model-swap endpoint — SOP assumes pod restart via kubectl. A graceful hot-swap mechanism would reduce cutover downtime
- [ ] FAISS flush + tracker reset (ADR-008) cannot be tested end-to-end until the MTMC service exists (Phase 2)

## Privacy & Compliance Gaps (P0-X02)

- [ ] No committed MinIO lifecycle policies — retention intent exists in Ansible vars but `infra/minio/lifecycle/` directory does not exist. Add lifecycle JSON for `raw-video`, `event-clips`, `thumbnails`
- [ ] DSAR export endpoint missing — no API for data subject access request packaging (admin-only, job-based, spans PostgreSQL + TimescaleDB + MinIO)
- [ ] Data subject deletion workflow missing — no admin-only deletion job with dry-run, approval metadata, and coordinated cross-store deletes
- [ ] Relational metadata retention is indefinite — `events`, `local_tracks`, `global_tracks`, `track_attributes` have no expiry. Add explicit retention jobs if customer contracts require hard ceilings
