# P2-PILOT01: 4-Camera CPU-Only Pilot Deployment — Handoff

## What was built

Complete deployment package for running the full Cilex Vision pipeline on a single Ubuntu 24 machine (Intel i5-13500, 16 GB RAM, no GPU).

### Deliverables

| File | Purpose |
|------|---------|
| `scripts/pilot/export_yolov8n_onnx.py` | Downloads YOLOv8n weights, exports to ONNX (dynamic batch, 640x640) |
| `infra/triton/model-repo/yolov8n/config.pbtxt` | Triton config: `onnxruntime_onnx`, KIND_CPU x2, max_batch 4, output [84,8400] |
| `infra/docker-compose.pilot.yml` | 14-container compose: single Kafka broker, all infra + all services |
| `infra/pilot/.env.pilot` | Environment template (DB, MinIO, Grafana, JWT credentials) |
| `infra/pilot/cameras.yaml` | 4-camera RTSP config (edit URLs before deployment) |
| `infra/prometheus/prometheus.pilot.yml` | Prometheus scrape config with targets for all 7 services + Triton |
| `scripts/pilot/setup-pilot.sh` | One-command setup: prereqs, model export, infra, topics, schema, seed |
| `scripts/pilot/add-camera.sh` | CLI to add a camera (RTSP test, YAML append, DB register) |
| `scripts/pilot/list-cameras.sh` | Show registered cameras with DB status and RTSP connectivity |
| `docs/deployment-guide-pilot.md` | Step-by-step guide, troubleshooting, upgrade paths |

## Key decisions

| Decision | Rationale |
|----------|-----------|
| Single Kafka broker (not 3) | Pilot fits on one machine. Replication factor = 1 for all topics. |
| Standard Triton image without GPU flags | `nvcr.io/nvidia/tritonserver:24.06-py3` runs on CPU when models use `KIND_CPU`. No nvidia-container-toolkit needed. |
| COCO-pretrained YOLOv8n (80 classes) | No retrained 7-class model available. Person (class 0) maps correctly. Other class indices mismatch but system stays stable (errors logged, frames skipped). |
| `num_classes=80` in inference worker env | Matches COCO model output. Post-processing naturally handles 80 class columns. |
| Separate `prometheus.pilot.yml` | Adds scrape targets for all services and Triton, vs production `prometheus.yml` which only scrapes Prometheus itself. |
| Bind-mount volumes under `infra/pilot-data/` | Named volumes are harder to inspect/backup. Bind mounts make data visible and portable. |
| Memory limits sum to ~11 GB | Leaves 4-5 GB for OS, Docker daemon, and container overhead within 16 GB. |
| Setup script uses `docker exec` for topic creation | Avoids requiring `confluent-kafka` Python package on the host. Uses Kafka's built-in CLI. |
| Alembic migration via temporary container | Runs `alembic upgrade head` from `services/db/` in a disposable `python:3.11-slim` container on the pilot network. |

## Memory budget

| Container | Limit |
|-----------|-------|
| TimescaleDB | 2 GB |
| Kafka (single broker) | 1.5 GB |
| Triton (CPU, 2 ONNX instances) | 2 GB |
| MinIO | 1 GB |
| Prometheus | 512 MB |
| 6 app services (edge-agent, ingress-bridge, decode, inference, bulk-collector, query-api) | 512 MB each = 3 GB |
| NATS, Redis, Grafana | 256 MB each = 768 MB |
| **Total** | **~10.8 GB** |

## Gotchas

- **COCO class mismatch**: The COCO model's class indices 4+ don't align with the taxonomy. `CLASS_INDEX_TO_NAME` in `detector_client.py` has entries for 0-6 only. Detections with index > 6 cause a `KeyError` caught by the per-message exception handler — logged and skipped, not fatal. For proper multi-class support, deploy a retrained 7-class model.
- **Ingress bridge security protocol**: Default is `SASL_SSL`. Pilot overrides to `PLAINTEXT` via env vars. Same for bulk-collector.
- **Decode service `decoded-frames` bucket**: Created by `minio-init` alongside `frame-blobs`, `debug-traces`, etc. Without it, the decode service fails to store decoded frames.
- **Triton startup time**: The standard Triton image is large (~15 GB). First pull takes 10-15 minutes. ONNX model loading adds 30-60 seconds on CPU. The healthcheck has a 60-second start_period.
- **Edge-agent YAML**: The `cameras.yaml` is loaded by `Settings.from_yaml()`. It must be valid YAML matching the `Settings` schema. Only `site_id` and `cameras` fields are set; other config (NATS, MinIO) comes from env vars.
- **OSNet (Re-ID) not available on CPU**: The model-repo only has `yolov8n`. Embedding extraction failures in the inference worker are caught and logged at DEBUG level. MTMC cross-camera matching won't work without an ONNX-exported osnet model.
- **No Schema Registry**: The pilot doesn't deploy Schema Registry. Services that reference it (ingress-bridge) fall back to direct protobuf serialization.

## Not done (out of scope)

- GPU deployment variant (documented in upgrade guide)
- TLS/mTLS for Kafka and NATS (pilot uses PLAINTEXT)
- Schema Registry deployment
- OSNet ONNX export for CPU Re-ID
- Kafka ACLs
- Automated RTSP URL env-var expansion in cameras.yaml
