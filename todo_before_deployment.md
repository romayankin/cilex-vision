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
- [ ] Query API still has no token issuance / login endpoint, so the new API docs rely on an external auth plane or locally minted JWTs for manual curl/Postman usage
- [ ] Query API still has no dedicated MTMC journey or `global_track_links` endpoint, so `docs/api/examples/get-journey.py` can only combine track detail, related events, and optional topology context instead of returning a true cross-camera journey
- [ ] `/topology/*` and `/debug/*` are role-gated but not filtered by `camera_scope`; confirm whether that is the intended security model before production exposure

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
- [ ] The `gpu-node` role overwrites `/etc/docker/daemon.json` to set `default-runtime: nvidia`. If Triton hosts need additional Docker daemon settings, merge them into managed config before production rollout

## Shadow Deploy Tooling Gaps (P3-V02)

- [ ] `scripts/shadow/compare_shadow.py` can only score confidence-distribution drift if Prometheus scrapes both `shadow_detection_confidence` and a future production-side `inference_detection_confidence` histogram. The current production inference worker still lacks that histogram
- [ ] The new shadow worker is not yet wired into any Prometheus scrape config. Add a scrape target before relying on `compare_shadow.py` against live shadow runs
- [ ] `infra/kafka/shadow-topics.yaml` reserves `embeddings.shadow`, but `scripts/shadow/shadow_inference_worker.py` currently shadows detector + tracker output only. Add the OSNet crop / publish path before using this tooling for Re-ID shadow evaluations

## Shadow Comparison Dashboard Gaps (P3-E02)

- [ ] `infra/prometheus/rules/shadow-alerts.yml` and `infra/grafana/dashboards/shadow-comparison.json` assume the shadow worker is scraped as Prometheus job `shadow-inference-worker`. Add or standardize that scrape target before relying on the dashboard or alerts during Stage 2 rollouts
- [ ] The dashboard includes a `$camera_id` drill-down variable for trace/log workflows, but the current production and shadow detection counters are not labeled by `camera_id`. Add camera-scoped shadow + production detection metrics before expecting true per-camera divergence panels

## Retraining Validation Gaps (P3-E01)

- [ ] The new validation harness can infer the candidate run from `models/latest_run_id.txt`, but there is still no canonical MLflow alias or config entry for the current production baseline run. Operators must supply `--baseline-run-id` / `BASELINE_RUN_ID` or a baseline JSON artifact until the registry has a stable "production detector" pointer

## Drift Monitoring Gaps (P3-E03)

- [ ] `infra/cron/drift-monitoring-cron.yml` is committed as an operator artifact only; it is not yet installed by Ansible or any deployment playbook
- [ ] The hourly detector assumes `s3://debug-traces/baselines/confidence-baseline.json` already exists. Baseline capture / refresh is still a manual operator workflow and should be rerun after approved model cutovers or major seasonal scene changes
- [ ] Drift monitoring currently queries the `detections` table, which already reflects the production confidence thresholding path. Confidence drift below the ingest threshold is invisible until raw detector confidences or a production confidence histogram are persisted

## Continuous Annotation Pipeline Gaps (P3-A01)

- [ ] `scripts/annotation/hard_example_miner.py` can only export examples that have both a low-confidence detection and a recoverable debug trace with `frame_uri`. Low-confidence detections that were not sampled into `debug-traces` are currently counted but cannot be sent to CVAT automatically
- [ ] `infra/cron/hard-example-mining-cron.yml` is committed as an operator artifact only; it is not yet installed by Ansible or any deployment playbook
- [ ] The new feedback loop writes a local retraining manifest at `data/training/raw/feedback-additions.json`, but no DVC/versioning or training playbook step consumes it automatically yet

## Re-ID Training Data Collection Gaps (P3-A02)

- [ ] `scripts/annotation/collect_reid_training_data.py` can only materialize representative crops for MTMC links that can be tied back to a debug trace carrying `frame_uri`. High-confidence `global_track_links` without trace-backed frame context are currently skipped from the triplet manifest
- [ ] `scripts/annotation/validate_reid_pairs.py` seeds CVAT review tasks and exports validation results, but the upload/export path was only linted and smoke-tested locally; run it once against the real CVAT deployment before relying on it for a sustained annotation queue
- [ ] `scripts/annotation/reid_dataset_builder.py` can build and optionally `dvc add` a local versioned Re-ID dataset, but nothing in the training pipeline consumes that manifest yet. A later Re-ID training task still needs to define the actual dataloader / triplet-loss input contract

## LPR Module Gaps (P4-V03)

- [ ] `tracklets.local` still lacks a canonical decoded-frame URI or frame-object lookup key, so `services/lpr-service/main.py` resolves representative frames heuristically from `decoded-frames` via MinIO `last_modified` plus fallback key templates. Add a frame-reference lookup table or persist `frame_uri` alongside track/detection metadata before relying on frame-accurate plate reads
- [ ] The repo still has no committed Triton model configs or engine-management wiring for `plate_detector` and `plate_ocr`. Add the actual model repository entries before enabling `LPR_ENABLED=true` in deployment
- [ ] Query API docs generated in `P2-X01` were not regenerated for the new `/lpr/results` endpoint, so `docs/api/openapi.yaml` and the committed Postman collection are now stale until the API documentation task is rerun or refreshed

## 100-Camera Load Test Gaps (P4-E01)

- [ ] `scripts/load-test/replay_streams.py` replays recorded video into the existing FrameRef + MinIO ingest contract; it does not emulate a true RTSP/NATS edge path. Add a dedicated RTSP/edge simulator if final Phase 4 sign-off must exercise the full edge transport chain instead of the central ingest contract only
- [ ] `scripts/load-test/chaos_scenarios.py` skips the GPU overload experiment unless operators provide `--overload-command` and optional cleanup wiring. Add a standard overload runner or deployment-aware helper before treating that scenario as automatic coverage
- [ ] `scripts/load-test/measure_e2e.py` and `generate_report.py` can report disk, network, GPU, and canonical end-to-end latency only when Prometheus scrapes metrics such as `container_fs_usage_bytes`, `container_network_*`, `nv_gpu_*`, and `e2e_latency_ms`. Add cAdvisor / DCGM exporters and the missing canonical latency metrics before using the 100-camera report as a final go/no-go artifact

## Zone Benchmark Gaps (P4-E02)

- [ ] `scripts/evaluation/zone_benchmark.py` is an offline synthetic benchmark that applies the zone-threshold and boundary-search rules with NumPy search pools; it does not yet drive the live `services/mtmc-service` zone-sharding modules or a real FAISS-backed MTMC deployment. Add a service-backed benchmark path if final large-site sign-off must validate runtime behavior instead of a deterministic proxy
- [ ] The benchmark hard-requires Python `mlflow` at execution time and no dedicated evaluation environment or requirements file is committed for `scripts/evaluation/`. Install MLflow in the operator/CI environment before relying on `P4-E02` automation for repeatable reports

## Multi-Site Infrastructure Gaps (P4-O01)

- [ ] `infra/ansible/playbooks/remove-site.yml` removes deployed scrape targets at runtime via `monitoring_excluded_hosts`, but it does not delete the site from inventory source files or generated inventory fragments. Add an inventory cleanup workflow before treating decommission as fully automated
- [ ] Site archival in `remove-site.yml` is prefix-based and therefore only captures objects that are already keyed by `site_id` or `camera_id`. Buckets with other key layouts still need a stronger metadata index or archive manifest workflow for complete site-level retention handling

## Commercial Readiness Gaps (P4-X01)

- [ ] The multi-site portal is now part of commercial collateral, but `frontend/app/portal/comparison/page.tsx` still uses derived/mock comparison metrics and the portal depends on site-management APIs that are not yet implemented server-side. Do not position live cross-site KPI dashboards or site CRUD as generally available until the backing `/sites` and real metrics APIs exist

## Architecture Contract Gaps (P4-X02)

- [ ] The canonical Kafka topic inventory and the active runtime are not fully aligned. `attributes.jobs` is still documented as a first-class lane, but the current `attribute-service` consumes `tracklets.local` and writes attributes directly to PostgreSQL. Decide whether to restore the explicit jobs topic or update the canonical contract before external architecture review
- [ ] `event-engine` currently both publishes `events.raw` and persists events directly, while the broader architecture story still implies a cleaner event-bus-to-storage separation. Either converge the runtime on one persistence pattern or document the hybrid path as intentional
- [ ] The architecture reference now documents `topology` as an architectural domain whose router is mounted inside `query-api`, not as a separately deployed API container. Keep external diagrams and deployment collateral aligned with that current runtime shape until a standalone topology service actually exists
- [ ] `archive.transcode.requested` and `archive.transcode.completed` remain provisioned topics, but a dedicated transcode worker is not part of the active 13-service deployment inventory. Avoid presenting the archive lane as fully staffed runtime automation until that worker exists

## Disaster Recovery Gaps (P4-O02)

- [ ] Kafka offset backup and restore remain operator-documented steps only. `P4-O02` added DB, MinIO, and config automation, but there is still no dedicated `kafka-consumer-groups` snapshot/restore script for the 15-minute offset RPO target
- [ ] `infra/backup/backup-db.sh` ships logical `pg_dump` backups only. If 15-minute PostgreSQL RPO must be guaranteed under sustained write load, add WAL archiving / PITR automation rather than relying on dump cadence alone

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
