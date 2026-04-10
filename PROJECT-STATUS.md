# PROJECT-STATUS.md
# 
# This file is the single source of truth for project state.
# Updated after every task completion. Read by every agent session.
# Referenced in CONVENTIONS.md, CLAUDE.md, and AGENTS.md.
#
# HOW TO USE:
# - Claude Code / Codex CLI: read this file FIRST before any task
# - Claude.ai conversations: upload this file to transfer context
# - Human developers: read this to understand current project state
#
# WHEN TO UPDATE:
# - After completing any task (mark done, add to completed section)
# - After making any architecture decision (add to decisions section)
# - After discovering any technical detail during implementation
# - After generating any document or artifact

## Project Identity

- **Name:** Cilex Vision
- **Repository:** https://github.com/romayankin/cilex-vision
- **Owner:** Roman Yankin (romayankin@gmail.com)
- **Purpose:** Multi-camera video analytics platform — edge acquisition → detection/tracking → cross-camera Re-ID → searchable events + clips
- **Market:** Commercial, outside EU/US

---

## Architecture Decisions (append-only — never delete, only supersede)

### ADR-001: Dual Message Bus
NATS JetStream at edge (lightweight, single binary, 50MB RAM) + Kafka at center (high throughput, consumer groups, compacted topics). Edge can't run Kafka (needs 3 brokers + 2-4GB RAM).

### ADR-002: Three Timestamps
source_capture_ts (camera clock, advisory/untrusted), edge_receive_ts (Chrony-synced, PRIMARY for cross-camera ordering), core_ingest_ts (Chrony-synced, ingest lag measurement).

### ADR-003: TimescaleDB for Detections
1-hour chunks, COPY protocol (100x faster than INSERT), chunk exclusion for <10ms queries, compression after 2 days (12-15x), retention drop after 30 days.

### ADR-004: FAISS Real-Time + pgvector Historical
FAISS in-memory flat index for 30-minute active matching horizon (<1ms). pgvector in PostgreSQL for 90-day historical similarity search (50-100ms at 6.5M vectors). Same PostgreSQL instance, single SQL join.

### ADR-005: Triton EXPLICIT Mode
No auto-load. Shadow deployment: load new version alongside old → compare 24-48hr → cutover → unload old. Prevents accidental model swaps.

### ADR-006: Camera-Agnostic via ONVIF + RTSP
No vendor lock-in. ONVIF Profile T for discovery, RTSP for stream. Dahua WizMind recommended for production, HiWatch DS-I402(D) for dev pilot.

### ADR-007: Protobuf Everywhere
All inter-service messages in Protobuf. Schema Registry for Kafka validation. buf lint for CI checks.

### ADR-008: Model Version Boundary
Never compare Re-ID embeddings across model versions. FAISS flush + tracker reset on OSNet cutover (~30s matching blackout). Accepted trade-off.

### ADR-009: Enums as CHECK-Constrained TEXT
Not PostgreSQL native ENUM (requires migration to add values). TEXT with CHECK constraint is more flexible.

---

## Technology Stack (38 technologies)

### Edge
- GStreamer (video decode, RTSP), NATS JetStream (edge broker), Python 3.11+ (edge agent)

### Transport
- Kafka 3.x (central broker, 3 brokers), Confluent Schema Registry (Protobuf validation), Protobuf (wire format)

### Processing
- Triton Inference Server (multi-model GPU hosting), TensorRT (ONNX→GPU engine), GStreamer NVDEC (hardware decode), ByteTrack (single-camera tracking), OSNet (Re-ID embeddings)

### Intelligence
- FAISS (real-time vector matching), ResNet-18 (color classification), FFmpeg + NVENC (clip extraction, archive transcode)

### Storage
- TimescaleDB (detection time-series), PostgreSQL 16 (relational), pgvector (historical embeddings), MinIO (S3-compatible object storage), Redis 7 (cache, rate limiting, coordination)

### Application
- FastAPI (query API), Pydantic (validation), Next.js (search UI, Phase 4), Tailwind CSS

### ML Operations
- CVAT (annotation), DVC (dataset versioning), PyTorch (training), MLflow (experiment tracking), Hydra (training config), ONNX (model export)

### Monitoring
- Prometheus (metrics), Grafana (5 dashboards), Chrony (NTP time sync)

### Security
- step-ca (internal PKI), mTLS (service-to-service auth), JWT + RBAC (user auth), bcrypt (password hashing)

### Infrastructure
- Docker Compose (local dev), Ansible (production deployment), buf (Protobuf linting/breaking checks)

---

## Task Status

### Phase 0 — Completed
| Task | Title | Branch | What it produced |
|------|-------|--------|-----------------|
| P0-D01 | Taxonomy & Requirements | feat/P0-D01 | docs/taxonomy.md (7 classes, attributes, events, NFRs, Mermaid state diagram), proto schemas (overlap with P0-D02) |
| P0-D02 | Protobuf Schema Package | feat/P0-D02 | 8 .proto files in proto/vidanalytics/v1/, buf.yaml, README, CI workflow. buf lint clean |
| P0-D03 | Kafka Topic Contract | feat/P0-D03 | docs/kafka-contract.md, infra/kafka/topics.yaml, create-topics.py |
| P0-D04 | Database Schema | feat/P0-D04 | 12 SQLAlchemy 2.0 models (2 hypertables + 10 relational), Alembic migration, Mermaid ER diagram, ADR-003 |
| P0-D05 | Camera Topology Graph Data Model | feat/P0-D05 | models.py (TopologyGraph, CameraNode with zone_id, TransitionEdge with per-class transit distributions), api.py (CRUD router), seed.py (4-camera demo site), topology-schema.json, 37 tests |
| P0-D06 | Ingress Bridge Spec | feat/P0-D06 | ADR-001 full spec (8 responsibilities, failure modes, metrics, deployment), ingress-bridge-flow.mermaid |
| P0-D07 | Time Sync Policy | feat/P0-D07 | Full timestamp policy doc, Chrony configs, clock_drift_check.py with mock smoke test, Prometheus alert rules |
| P0-D08 | Security Design | feat/P0-D08 | Full security spec (trust model, PKI, NATS mTLS, Kafka SASL_SSL, ACL matrices), step-ca config, bootstrap-site.sh, NATS/Kafka templates |
| P0-D09 | Model Rollout & Cutover SOP | feat/P0-D09 | docs/runbooks/model-rollout-sop.md (4-stage SOP: offline qualification, shadow deployment, site-level cutover with ADR-008 FAISS flush, post-cutover watch with 15% rollback trigger) |
| P0-D10 | Triton Placement | feat/P0-D10 | Full placement spec (model inventory, VRAM budget, GPU classes, co-location), 3 Triton config.pbtxt files, triton-alerts.yml |
| P0-O01 | Infrastructure Scaffolding | feat/P0-O01 | docker-compose.yml (Kafka 3-broker, NATS, TimescaleDB, MinIO, Redis, Prometheus, Grafana, MLflow), CI workflow, Makefile |
| P0-E01 | Bake-Off Protocol | feat/P0-E01 | Full bake-off protocol (detector/tracker/attribute), run_detector_bakeoff.py, compare_bakeoff.py |
| P0-V01 | Throwaway 1-Camera Prototype | feat/P0-V01 | demo.py (382 lines, RTSP/webcam → YOLOv8n → SQLite → Flask MJPEG + detection table + chart), Dockerfile, README (marked disposable) |
| P0-X01 | Parametric Cost Model | feat/P0-X01 | Extended params.yaml with cost_model section (all values marked REPLACE WITH MEASURED), cost_model.py (P25/P50/P90 × 4/10/100, stdout tables + Excel via openpyxl), 5 tests |
| P0-X02 | Privacy & Compliance Framework | feat/P0-X02 | docs/privacy-framework.md (data classification table, RBAC permissions matrix aligned to 4 existing roles, architectural hooks checklist with gap analysis, DPIA trigger criteria with EDPB/ICO/PDPC references) |


### Phase 0 — Remaining (priority order)
| Task | Title | Unblocks | Priority |
|------|-------|----------|----------|
| P0-D05 | Edge Filter Design | — | 4 |
| P0-D09 | Privacy Framework | — | 4 |
| P0-V01 | Throwaway Prototype | — | 4 |
| P0-X01 | Camera Compat Matrix | — | 4 |
| P0-X02 | Hardware Sizing | — | 4 |

### Phase 1 — Completed
| Task | Title | Branch | What it produced |
|------|-------|--------|-----------------|
| P1-O01 | Pilot Infrastructure | feat/P1-O01 | 13 Ansible playbooks (Kafka, NATS, TimescaleDB, MinIO, Triton, monitoring, MLflow, CVAT, services, topics, smoke-test), pilot inventory, templates, common role |
| P1-V01 | Edge Agent | feat/P1-V01 | 9 Python modules (main, camera_pipeline, motion_detector, rtsp_client, nats_publisher, local_buffer, config, metrics, gen_proto), Dockerfile, 23 tests, handoff |
| P1-A01 | CVAT Setup & Annotation Baseline | feat/P1-A01 | setup_cvat_projects.py (3 CVAT projects), annotation-guidelines.md, compute_iaa.py (IAA scorecard), split_dataset.py (temporal split), 3 SVG diagrams |
| P1-E01 | Detector Bake-Off (10 days) | feat/P1-E01 | Published-benchmark proxy comparison, enhanced run_detector_bakeoff.py (git state, dataset metadata, operational slice), enhanced compare_bakeoff.py (offline JSON mode, SVG charts), detector-comparison.md recommending YOLOv8-L |
| P1-E02 | Tracker Bake-Off (5 days) | feat/P1-E02 | run_tracker_bakeoff.py (MOT harness with local ByteTrack + external BoT-SORT support), generalized compare_bakeoff.py (detector+tracker phases), tracker-comparison.md recommending BoT-SORT, 3 SVG charts |
| P1-E03 | Edge Filter Pass-Through Measurement | feat/P1-E03 | edge_filter_calibration.py (NATS capture, offline detector+motion replay, grid search, scorecard), schedule_calibration.py (camera rotation, 1/hr rate limit), params.yaml, 6 tests |
| P1-V02 | Ingress Bridge Service | feat/P1-V02 | main.py (NATS→Kafka bridge with spool, schema validation, blob offload, DLQ), config.py, metrics.py (18 Prometheus instruments), Dockerfile, 6 tests including chaos scenario |
| P1-V03 | Central Decode & Frame Sampling | feat/P1-V03 | decoder.py (Pillow JPEG + GStreamer H.264/H.265), color_space.py (BT.601/709 detection + YCbCr→RGB), sampler.py (per-camera FPS gating), publisher.py, Dockerfile with GStreamer, 51 tests |
| P1-V04 | Detection & Tracking Inference Worker | feat/P1-V04 | main.py (Kafka pipeline), detector_client.py (Triton YOLOv8-L + NMS), tracker.py (ByteTrack), embedder_client.py (OSNet Re-ID), publisher.py (3 Kafka topics), debug_trace.py (1-5% sampling), Dockerfile, 37 tests |
| P1-V05 | Metadata Bulk Collector | feat/P1-V05 | main.py (Kafka→TimescaleDB COPY), collector.py (commit-safe batching), writer.py (asyncpg COPY + dedup), Dockerfile, 6 tests, load-test-collector.py (zero-loss assertion) |
| P1-V06 | Basic Query API | feat/P1-V06 | FastAPI app (main.py, 3 routers, JWT httpOnly auth, RBAC 4 roles, camera scope filtering, audit middleware, asyncpg raw SQL, signed MinIO URLs), Dockerfile, 36 tests |
| P1-V07 | Debug Trace Pipeline | feat/P1-V07 | Enhanced debug_trace.py (TraceCollector with pre-NMS capture, tracker delta, 30d MinIO lifecycle), query-api routers/debug.py (engineering-only, signed URLs), 42 new tests |
| P1-X01 | Camera Compatibility Matrix | feat/P1-X01 | docs/camera-compatibility.md (Dahua WizMind + Hikvision ColorVu seeded as UNTESTED), probe_camera.py (ONVIF+RTSP live probe + published-only mode), run_compat_suite.sh (CSV/YAML inventory → JSON reports + Markdown matrix) |


### Phase 1 — Pending (12 tasks remaining)

### Phase 2 — Completed
| Task | Title | Branch | What it produced |
|------|-------|--------|-----------------|
| P2-FIX01 | Pilot Readiness Fixes | feat/P2-FIX01 | Canonical Kafka topic fixes, inference-worker trace wiring, query-api topology router wiring, and regression tests for those gaps |
| P2-E01 | Attribute Classifier Bake-Off | feat/P2-E01 | CVAT-to-crop data prep script, attribute bake-off harness with Triton/ONNXRuntime inference and MLflow logging, and a proxy comparison report recommending the safe-default ResNet-18 until real eval data exists |
| P2-E02 | MTMC Evaluation | feat/P2-E02 | Re-ID ground-truth export validation script, pure cross-camera metric computation helpers, an asyncpg + MLflow MTMC evaluation harness, and a proxy report documenting the go/no-go gate plus current GT export gaps |
| P2-E03 | End-to-End Stress Test | feat/P2-E03 | End-to-end load-test harness with synthetic/replay frame generation, Prometheus snapshot collection, reversible chaos scenarios, and a Markdown NFR report template |
| P2-O01 | MTMC Infrastructure | feat/P2-O01 | Dedicated `osnet_reid` Triton model config, MTMC Ansible deployment playbook, MTMC Grafana health dashboard, Prometheus alerts, and monitoring scrape wiring |
| P2-O02 | Storage Tiering | feat/P2-O02 | Canonical MinIO lifecycle policy JSON, idempotent `mc`-based lifecycle apply/report scripts, a storage-tiering Grafana dashboard, MinIO capacity/ILM alerts, and MinIO metrics scrape wiring for pilot + Ansible monitoring |
| P2-O03 | Calibration Scheduler | feat/P2-O03 | DB-driven calibration rotation using online cameras from PostgreSQL, runtime `calibration_results` persistence, per-camera calibration trend reporting, and a cron/systemd automation definition |
| P2-PILOT01 | 4-Camera CPU-Only Pilot Deployment | feat/P2-PILOT01 | Pilot compose stack, YOLOv8n CPU Triton model wiring, setup scripts, camera helpers, pilot Prometheus config, and deployment guide |
| P2-X02 | Operations Runbooks | feat/P2-X02 | Five operator-facing runbooks covering incident response, scaling, backup and restore, camera onboarding, and service restart procedures aligned to the deployed pilot and multi-node stacks |
| P2-V01 | Attribute Extraction Service | — | Attribute service consuming `tracklets.local`, quality gate + white balance + Triton color classifier, asyncpg persistence, Dockerfile, strict mypy config, and 18 tests |
| P2-V02 | MTMC Re-ID Association Service | feat/P2-V02 | MTMC service with FAISS matching, topology-aware scoring, checkpoint/restore, asyncpg persistence, Dockerfile, strict mypy config, and 13 tests |
| P2-V03 | Event Engine | feat/P2-V03 | Event-engine service with per-track FSMs, Kafka + PostgreSQL event publishing, ROI / loitering polygon parsing from camera config, Dockerfile, strict mypy config, and 14 tests |
| P2-V04 | Clip Pipeline | feat/P2-V04 | Clip-service consuming closed `events.raw` records, FFmpeg H.264 baseline clip extraction, thumbnail generation, MinIO upload, PostgreSQL asset updates, Dockerfile, strict mypy config, and 6 tests |
| P2-V05 | Search UI & Timeline | feat/P2-V05 | Next.js frontend with search, camera timeline, cross-camera journey, admin views, API proxy client, HLS/MP4 playback, and standalone Docker packaging |
| P2-X01 | API Documentation | feat/P2-X01 | Auto-generated Query API OpenAPI YAML, API README, runnable curl/Python examples, Postman collection generator, and committed Postman collection for the current FastAPI route surface |

### Phase 2 — Pending (0 tasks remaining)

### Phase 3 — Completed
| Task | Title | Branch | What it produced |
|------|-------|--------|-----------------|
| P3-O01 | Deployment Automation | feat/P3-O01 | Terraform modules for multi-node compute/network/storage provisioning, multi-node Ansible deployment orchestration with GPU and edge roles, a production inventory template, and a deployment health-check script |
| P3-V02 | Shadow Deploy Tooling | feat/P3-V02 | Shadow Triton repo docs, separate shadow Kafka topic definitions, a urllib-based EXPLICIT mode load/unload script, a detector-shadow inference worker publishing to shadow topics, and a Prometheus-backed comparison report script |
| P3-E01 | Retraining Validation | feat/P3-E01 | Pure regression-comparison helpers, an MLflow/JSON retrained-vs-production validation harness with Markdown + JSON artifacts, and a `make validate` target for the training pipeline |
| P3-E02 | Shadow Comparison Dashboard | feat/P3-E02 | Grafana shadow-vs-production rollout dashboard with divergence, latency, class mix, and worker-health panels plus Prometheus alert rules for shadow deployment anomalies |
| P3-E03 | Drift Monitoring | feat/P3-E03 | TimescaleDB-backed confidence baseline snapshotting, KS/KL drift detection with Prometheus textfile output, and an hourly cron/systemd artifact for production drift scans |

### Phase 3 — Pending (8 tasks remaining)
### Phase 4 — Pending (12 tasks remaining)

---

## Component Details (condensed — full descriptions in generated PDFs)

### Core Pipeline (20 components)
1. **Cameras** — RTSP/ONVIF over isolated VLAN, PoE, no internet
2. **Edge Agent** — Python + GStreamer, motion filter (frame diff + scene change), ~15% pass-through
3. **NATS JetStream** — Edge broker, 10GB/24h buffer, mTLS, per-site permissions
4. **Ingress Bridge** — Schema validation + NVMe spool (50GB) for Kafka outages, live+replay lanes
5. **Kafka** — 7 topics, 3 brokers RF=3, camera_id/track_id/event_id partition keys
6. **Decode Service** — Per-codec GStreamer/NVDEC, BT.601/709 color space handling, normalize to 1280×720 RGB
7. **Triton** — EXPLICIT mode, 4 models on 24GB GPU (~420MB VRAM), dynamic batching
8. **ByteTrack** — Two-stage association (high+low confidence), Kalman filter, 30-frame patience
9. **OSNet Re-ID** — 512-dim L2-normalized embeddings, quality gate, model version boundary
10. **Attribute Service** — 6-stage color classification, IR detection, confidence-weighted voting
11. **MTMC** — 5-stage scoring (topology→transit→FAISS→attributes→combined), 0.65 threshold
12. **Event Engine** — 6 event types, rule-based state machines, zone point-in-polygon, dedup+suppression
13. **Clips** — Stream copy or NVENC re-encode, thumbnail extraction, 90s cap
14. **Transcode** — Hot→warm tier (70% reduction), hevc_nvenc, verify before delete
15. **TimescaleDB** — 1h chunks, COPY protocol, compression 12-15x, 30d retention
16. **PostgreSQL** — 10 tables, pgvector for historical Re-ID (ivfflat, 50-100ms at 6.5M vectors)
17. **MinIO** — 5 buckets (frame-blobs/event-clips/thumbnails/debug-traces/archive-warm), signed URLs 1hr
18. **Redis** — Rate limiting, JWT blacklist, camera health, computation cache, 256MB max
19. **FastAPI** — JWT httpOnly cookie, RBAC 4 roles, camera scope filtering, audit logging 2yr
20. **Next.js** — Phase 4: search, timeline, journey, admin views

### Supporting Systems (13 components)
1. **CVAT** — Annotation: active learning + diversity + failure mining frame selection, pre-annotation, honeypots + Cohen's kappa QC
2. **DVC** — Dataset versioning: pointer files in Git, data in MinIO content-addressed, dvc.yaml pipelines
3. **PyTorch** — Retraining: backbone freeze 10 epochs, FP16, augmentations, early stopping, triplet loss for Re-ID
4. **MLflow** — Experiment tracking + model registry (None→Staging→Production→Archived)
5. **ONNX** — Export: dynamic_axes, opset 17, verification max diff <0.0001
6. **TensorRT** — 6 optimizations: fusion, FP16, kernel tuning, memory planning, format, scheduling. 5.8x speedup
7. **Triton Deploy** — Shadow deployment lifecycle: load alongside → compare 24-48hr → cutover → watch → rollback if needed
8. **Prometheus** — 100+ metrics, 15s scrape, alerts for VRAM/queue/lag/camera/spool/clock/cert
9. **Grafana** — 5 dashboards: stream health, inference perf, bus health, storage, model quality
10. **Chrony** — 3 NTP pools, drift collector pairwise clock_skew_ms, 500ms WARN / 2000ms CRITICAL
11. **step-ca** — Internal PKI, 90-day certs, daily auto-renewal, CRL revocation
12. **mTLS** — Bidirectional auth, NATS verify_and_map CN→subject ACLs, Kafka SASL_SSL with SCRAM-SHA-256 → topic ACLs, closed trust chain
13. **JWT+RBAC** — httpOnly cookie, 4 roles, camera scope WHERE IN, audit 2yr, bcrypt+rate limiting

---

## Documents Generated
| File | Pages | Contents |
|------|-------|---------|
| docs/bakeoff-results/attribute-comparison.md | — | Proxy attribute bake-off report documenting the safe-default recommendation until real CVAT evaluation data and candidate artifacts exist |
| docs/evaluation-results/mtmc-evaluation.md | — | Proxy MTMC evaluation report documenting the go/no-go threshold, scoring method, and current ground-truth export blockers until a real run is executed |
| cilex-vision-pipeline-reference.pdf | 35 | Core pipeline: 20 components with clickable diagram |
| cilex-vision-pipeline-reference-ru.pdf | 25 | Russian translation of pipeline reference |
| cilex-vision-supporting-systems.pdf | 19 | Supporting systems: 13 components (CONDENSED — needs full rebuild) |
| cilex-vision-architecture-diagrams.pdf | 6 | Core pipeline + supporting systems diagrams |
| build_supporting_systems_pdf.py | — | Builder script for full ~80-100 page supporting systems PDF |
| project-plan-ai-agent-driven.pdf | 45 | Project plan EN |
| project-plan-ai-agent-driven-ru.pdf | 44 | Project plan RU |
| project-plan-enhanced-v2.pdf | 47 | Enhanced plan with agent prompts |
| ai-agent-orchestration-guide.pdf | 41 | Agent setup guide |
| cilex-vision-tech-stack.pdf | 16 | Tech stack (38 technologies) |
| camera-comparison.pdf | — | Camera comparison EN + RU |
| docs/runbooks/{incident-response,scaling,backup-restore,camera-onboarding,service-restart}.md | — | Phase 2 operator runbooks for alert response, capacity changes, backup and restore, camera onboarding, and controlled restarts |
| docs/api/{openapi.yaml,README.md,postman-collection.json} | — | Auto-generated Query API contract plus operator-facing authentication, pagination, signed-URL, and Postman usage guide |

---

## Bugs Fixed in Tooling
1. launch.sh `&` in task titles broke bash eval → line-by-line parsing
2. review.sh showed "no files changed" → combine committed + uncommitted + untracked
3. review.sh Design write-zone false positives → expanded zone, downgraded to WARN
4. Missing 12 stub files referenced by role configs
5. Missing 18 of 29 prompt files
6. Context loss between sessions → CONVENTIONS.md + handoff notes
7. PDF xlink:href links not working → HTML overlay `<a href="#id">` positioned over SVG

---

## Agent Workflow
```
.agents/status.sh              # see what's ready
.agents/launch.sh P0-XXX       # create branch, build context
claude --model opus             # "Read CONVENTIONS.md then .claude-task-context.md"
git add -A && git commit
.agents/review.sh P0-XXX       # automated quality check

# DELIVERABLE AUDIT (mandatory — compare prompt to actual output):
cat .agents/prompts/P0-XXX.md
git diff --name-only main...feat/P0-XXX
# Every file listed in the prompt must appear in the diff.
# If anything is missing, send feedback to the agent before merging.

# VERIFY before merge:
git status                     # must be clean
git log --oneline feat/P0-XXX ^main   # must show commits ahead of main

git checkout main && git merge feat/P0-XXX

# VERIFY after merge:
git diff HEAD~1 --stat         # must show expected files changed

# POST-COMPLETION (both steps mandatory — never skip):
# 1. Update manifest
python3 -c "
import yaml
with open('.agents/manifest.yaml') as f:
    m = yaml.safe_load(f)
for phase in m['phases'].values():
    for task in phase['tasks']:
        if task['id'] == 'P0-XXX':
            task['status'] = 'done'
with open('.agents/manifest.yaml', 'w') as f:
    yaml.dump(m, f, default_flow_style=False, sort_keys=False)
"

# 2. Update PROJECT-STATUS.md — move task to Completed table with what it produced

git add .agents/manifest.yaml PROJECT-STATUS.md
git commit -m "status: mark P0-XXX done"
git push

.agents/status.sh              # see what unlocked
# 3. Check handoff for deployment TODOs
# Read .agents/handoff/P0-XXX.md for gaps, missing wiring, or prerequisites
# Append any findings to todo_before_deployment.md
```

---

## User Preferences
- GH_TOKEN: (set in environment, not committed)
- Railpacks (never nixpacks) on railway.app
- Ubuntu 24.04.4 LTS, project at ~/projects/cilex-vision
- claude --model opus with /effort max for design tasks
- buf installed at /usr/local/bin/buf
