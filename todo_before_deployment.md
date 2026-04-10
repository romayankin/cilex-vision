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

## Calibration Scheduler Gaps (P2-O03)

- [ ] `scripts/calibration/calibration_scheduler.py` chooses cameras from PostgreSQL, but execution still depends on `--edge-config` for site/NATS/MinIO/motion defaults because the current `cameras` table does not carry enough runtime calibration config for a DB-only workflow
- [ ] `calibration_results` is created lazily by the scheduler/report scripts at runtime; add an Alembic migration or deployment-time DDL step before relying on this in environments where the app DB user lacks `CREATE TABLE` / `CREATE INDEX` privileges
- [ ] `infra/cron/calibration-cron.yml` is committed as an operator artifact only; it is not yet installed by Ansible or any deployment playbook

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

## Stress-Test Observability Gaps (P2-E03)

- [ ] The taxonomy NFRs reference `e2e_latency_ms` and `inference_fps{camera_id}`, but the current services do not export those exact Prometheus metrics. The new stress-test harness reports that as failed validation; add the canonical metrics before relying on NFR pass/fail output for pilot sign-off
- [ ] The current Query API `/health` endpoint returns only `{"status": "ok"}` and does not expose active camera / stream count, so the stress-test harness cannot validate the "4 active pilot cameras" NFR from a live health contract yet
- [ ] `infra/prometheus/prometheus.pilot.yml` still scrapes only the original pilot services plus Triton. If attribute-service, event-engine, clip-service, or mtmc-service are deployed for a full Phase 2 pilot, add scrape targets before expecting complete end-to-end stress-test coverage
- [ ] The repo still has no committed replay media corpus for realistic event / clip / MTMC-heavy stress runs. Operators must currently provide `--replay-frame-dir` inputs out-of-band for those scenarios

## Attribute Bake-Off Gaps (P2-E01)

- [ ] `data/eval/attribute/manifest.json` is still absent in the repo, so `scripts/bakeoff/run_attribute_bakeoff.py` cannot produce a real comparison yet. Annotated `attribute-eval` CVAT tasks still need to be exported with `scripts/bakeoff/prepare_color_eval_data.py`
- [ ] The repository still has no committed `artifacts/models/attribute/efficientnet_b0.onnx` export, so the EfficientNet-B0 challenger path remains evaluation-ready in code only

## MTMC Evaluation Gaps (P2-E02)

- [ ] `scripts/annotation/export_reid_pairs.py` still emits synthetic `local_track_id` values derived from CVAT shape IDs. Real MTMC evaluation requires DB-backed `local_tracks.local_track_id` UUIDs in the exported ground truth
- [ ] The MTMC service persists only final `global_track_id` assignments, not the ranked candidate lists considered during matching. `P2-E02` therefore reports Rank-1 / Rank-5 / mAP as assignment-derived proxies until candidate-level retrieval outputs are persisted

## Operations Runbook Gaps (P2-X02)

- [ ] `attribute-service`, `event-engine`, and `clip-service` do not have a standard host-reachable `/health` or `/ready` contract in the shared deployment path. Operators currently have to rely on container logs and Prometheus scrape presence during restart verification
- [ ] Topology administration still depends on either an environment-specific admin JWT cookie for the Query API or direct SQL fallback. Add a first-class operator login / admin workflow before large-scale camera onboarding

## Deployment Automation Gaps (P3-O01)

- [ ] `infra/ansible/inventory/production.yml` is intentionally a template: replace the placeholder IPs, PKI source paths, credentials, and empty `service_deployments` entries before running `deploy-multi-node.yml`
- [ ] The new Terraform modules were syntax-reviewed but could not be `terraform fmt` / `terraform validate` checked locally because the Terraform CLI is not installed in this environment. Run `terraform init`, `terraform fmt`, and `terraform validate` in CI or an operator workstation before first apply
- [ ] The `gpu-node` role overwrites `/etc/docker/daemon.json` to set `default-runtime: nvidia`. If Triton hosts need additional Docker daemon settings, merge them into managed config before production rollout

## Shadow Deploy Tooling Gaps (P3-V02)

- [ ] `scripts/shadow/compare_shadow.py` can only score confidence-distribution drift if Prometheus scrapes both `shadow_detection_confidence` and a future production-side `inference_detection_confidence` histogram. The current production inference worker still lacks that histogram
- [ ] The new shadow worker is not yet wired into any Prometheus scrape config. Add a scrape target before relying on `compare_shadow.py` against live shadow runs
- [ ] `infra/kafka/shadow-topics.yaml` reserves `embeddings.shadow`, but `scripts/shadow/shadow_inference_worker.py` currently shadows detector + tracker output only. Add the OSNet crop / publish path before using this tooling for Re-ID shadow evaluations

## Model Rollout SOP Gaps (P0-D09)

- [ ] No automated rollout orchestration — SOP is manual copy-paste commands. Consider an Ansible playbook or rollout script to reduce human error during model cutover
- [ ] Inference worker has no in-process model-swap endpoint — SOP assumes pod restart via kubectl. A graceful hot-swap mechanism would reduce cutover downtime
- [ ] FAISS flush + tracker reset (ADR-008) cannot be tested end-to-end until the MTMC service exists (Phase 2)

## Privacy & Compliance Gaps (P0-X02)

- [x] Commit MinIO lifecycle policies — `P2-O02` added `infra/minio/lifecycle-policies.json` plus apply/report tooling for the retention buckets
- [ ] DSAR export endpoint missing — no API for data subject access request packaging (admin-only, job-based, spans PostgreSQL + TimescaleDB + MinIO)
- [ ] Data subject deletion workflow missing — no admin-only deletion job with dry-run, approval metadata, and coordinated cross-store deletes
- [ ] Relational metadata retention is indefinite — `events`, `local_tracks`, `global_tracks`, `track_attributes` have no expiry. Add explicit retention jobs if customer contracts require hard ceilings

## Storage Tiering Gaps (P2-O02)

- [ ] `infra/ansible/playbooks/deploy-minio.yml` still bootstraps buckets only; it does not yet run `infra/minio/apply-lifecycle.py`. Add an idempotent playbook task or operator SOP step before relying on MinIO retention enforcement in fresh deployments
- [ ] `scripts/cost-model/params.yaml` still lacks a dedicated `cold_object` monthly rate, so `infra/minio/storage-report.py` and the `storage-tiering` dashboard price cold buckets with the warm-tier proxy until the cost model is extended
