# P2-PILOT01: 4-Camera CPU-Only Pilot Deployment

You are working across /repo. Create a complete CPU-only deployment for the full Cilex Vision pipeline running on a single Ubuntu 24 machine with an Intel i5-13500 (20 threads), 16GB RAM, no GPU.

## 1. YOLOv8n ONNX Model for Triton CPU

Create `scripts/pilot/export_yolov8n_onnx.py`:
- Downloads ultralytics YOLOv8n weights
- Exports to ONNX format with dynamic batch, input size 640x640
- Saves to `infra/triton/model-repo/yolov8n/1/model.onnx`

Create `infra/triton/model-repo/yolov8n/config.pbtxt`:
- Platform: `onnxruntime_onnx`
- Input: `images` float32 [1,3,640,640]
- Output: `output0` float32 [1,84,8400] (YOLOv8n shape)
- Instance group: KIND_CPU, count 2
- Dynamic batching enabled, max_batch_size 4

## 2. Docker Compose for CPU-Only Pilot

Create `infra/docker-compose.pilot.yml`:
- All infrastructure: NATS, Kafka (single broker), TimescaleDB, MinIO, Redis, Prometheus, Grafana
- Triton server (CPU mode, NO --gpus flag, mounting the model-repo with yolov8n ONNX)
- All services: edge-agent, ingress-bridge, decode-service, inference-worker, bulk-collector, query-api
- Memory limits per container to fit in 16GB total:
  - TimescaleDB: 2GB
  - Kafka: 1.5GB
  - MinIO: 1GB
  - Triton: 2GB
  - Each app service: 512MB
  - Prometheus: 512MB
  - Grafana: 256MB
- Volumes for persistent data under `./pilot-data/` (minio, postgres, kafka, prometheus)
- Environment variables referencing a single `.env.pilot` file
- Network: single bridge network `cilex-pilot`

## 3. Pilot Configuration

Create `infra/pilot/.env.pilot` with all required environment variables:
- NATS, Kafka, MinIO, TimescaleDB connection strings (all localhost/container names)
- Edge agent: camera list from `infra/pilot/cameras.yaml`
- Inference worker: `input_topic=frames.decoded.refs`, detector model `yolov8n`, confidence threshold 0.35 (lower for nano model)
- Debug tracing: enabled, sample rate 5%

Create `infra/pilot/cameras.yaml` — example 4-camera config:
```yaml
site_id: pilot-site
cameras:
  - camera_id: cam-1
    rtsp_url: "${CAM1_URL:-rtsp://admin:admin@192.168.1.100/stream1}"
    enabled: true
  - camera_id: cam-2
    rtsp_url: "${CAM2_URL:-rtsp://admin:admin@192.168.1.101/stream1}"
    enabled: true
  - camera_id: cam-3
    rtsp_url: "${CAM3_URL:-rtsp://admin:admin@192.168.1.102/stream1}"
    enabled: true
  - camera_id: cam-4
    rtsp_url: "${CAM4_URL:-rtsp://admin:admin@192.168.1.103/stream1}"
    enabled: true
```

## 4. Setup Script

Create `scripts/pilot/setup-pilot.sh`:
- Checks Docker and Docker Compose are installed
- Checks available RAM (warn if <16GB)
- Checks available disk (warn if <50GB)
- Runs `export_yolov8n_onnx.py` to download and export the model (if model.onnx doesn't exist)
- Creates Kafka topics from `infra/kafka/topics.yaml`
- Creates MinIO buckets (frame-blobs, event-clips, debug-traces, thumbnails)
- Applies DB schema to TimescaleDB
- Seeds topology from `services/topology/seed.py`
- Prints "Pilot ready" with URLs for Query API, Grafana, MinIO console

## 5. Camera Management Script

Create `scripts/pilot/add-camera.sh`:
- Usage: `add-camera.sh --id cam-5 --url rtsp://admin:pass@192.168.1.104/stream1`
- Tests RTSP connectivity (OpenCV or ffprobe)
- Adds to `infra/pilot/cameras.yaml`
- Registers in TimescaleDB cameras table
- Prints instructions to restart edge-agent

Create `scripts/pilot/list-cameras.sh`:
- Shows all registered cameras with status (from DB)
- Shows RTSP connectivity check result for each

## 6. Inference Worker CPU Compatibility

The inference worker currently uses `detector_client.py` which talks to Triton via gRPC. Ensure:
- The Triton gRPC endpoint works with the ONNX CPU backend (it should — just verify config.pbtxt is correct)
- If the inference worker has any GPU-specific code paths, add CPU fallbacks
- The tracker (ByteTrack) is already pure Python/numpy — no GPU dependency

## 7. Deployment Guide

Create `docs/deployment-guide-pilot.md`:
- Target: Ubuntu 24, Intel i5+, 16GB+ RAM, no GPU required
- Prerequisites: Docker, Docker Compose, Python 3.12, 50GB free disk
- Step-by-step:
  1. Clone repo
  2. Run setup-pilot.sh
  3. Edit cameras.yaml with your camera IPs
  4. Run `docker compose -f infra/docker-compose.pilot.yml up -d`
  5. Verify all services healthy
  6. Open Grafana at :3000 — check stream-health dashboard
  7. Open Query API at :8080/docs — try querying detections
  8. Add/remove cameras with add-camera.sh
- Troubleshooting section: common RTSP issues, Triton not loading model, Kafka consumer lag
- Performance expectations: ~10 FPS inference on CPU, 4 cameras at 15% duty = ~6 FPS needed
- How to upgrade to GPU later

## Constraints

- Must work on the user's actual machine: i5-13500, 16GB RAM, 20GB free disk, no GPU, Ubuntu 24.04, Docker 29.3
- All containers must fit in 16GB RAM total
- No NVIDIA runtime/toolkit required
- Triton must use ONNX backend on CPU only
- Reuse existing services from the repo — do not rewrite them. Only create deployment configs and scripts.

## Expected Deliverables

- scripts/pilot/export_yolov8n_onnx.py
- infra/triton/model-repo/yolov8n/config.pbtxt
- infra/docker-compose.pilot.yml
- infra/pilot/.env.pilot
- infra/pilot/cameras.yaml
- scripts/pilot/setup-pilot.sh
- scripts/pilot/add-camera.sh
- scripts/pilot/list-cameras.sh
- docs/deployment-guide-pilot.md
- .agents/handoff/P2-PILOT01.md
