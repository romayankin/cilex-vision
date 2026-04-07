# Cilex Vision Pilot Deployment Guide

CPU-only, 4-camera deployment on a single machine.

## Target hardware

- Ubuntu 24.04 LTS
- Intel i5-13500 (or comparable: 12+ threads recommended)
- 16 GB RAM minimum
- 50 GB free disk
- No GPU required

## Prerequisites

| Software | Version | Check |
|----------|---------|-------|
| Docker | 24+ | `docker --version` |
| Docker Compose | v2 (bundled with Docker) | `docker compose version` |
| Python | 3.11+ | `python3 --version` |
| Git | any | `git --version` |

## Quick start

```bash
# 1. Clone
git clone <repo-url> cilex-vision
cd cilex-vision

# 2. Edit camera IPs
nano infra/pilot/cameras.yaml

# 3. Run setup (downloads model, starts infra, applies schema)
bash scripts/pilot/setup-pilot.sh

# 4. Verify
docker ps --format "table {{.Names}}\t{{.Status}}"
```

Setup takes 5-10 minutes on first run (Docker image pulls + ONNX export).

## Step-by-step

### 1. Configure cameras

Edit `infra/pilot/cameras.yaml` with your RTSP URLs:

```yaml
site_id: pilot-site
cameras:
  - camera_id: cam-1
    rtsp_url: "rtsp://admin:password@192.168.1.100/stream1"
    enabled: true
  - camera_id: cam-2
    rtsp_url: "rtsp://admin:password@192.168.1.101/stream1"
    enabled: true
```

Common RTSP URL patterns:
- Hikvision: `rtsp://admin:pass@IP:554/Streaming/Channels/101`
- Dahua: `rtsp://admin:pass@IP:554/cam/realmonitor?channel=1&subtype=0`
- ONVIF generic: `rtsp://admin:pass@IP:554/stream1`

### 2. Configure credentials

Copy and edit the environment file:

```bash
cp infra/pilot/.env.pilot infra/.env
nano infra/.env
```

Change at minimum:
- `POSTGRES_PASSWORD` - database password
- `MINIO_ROOT_PASSWORD` - object store password
- `JWT_SECRET` - API authentication secret

### 3. Run setup

```bash
bash scripts/pilot/setup-pilot.sh
```

This script:
1. Checks Docker, RAM (warns <16 GB), disk (warns <50 GB)
2. Exports YOLOv8n to ONNX for Triton CPU inference
3. Starts infrastructure containers (Kafka, NATS, TimescaleDB, MinIO, Triton, Prometheus, Grafana)
4. Creates all Kafka topics
5. Applies database schema via Alembic migrations
6. Seeds the 4-camera topology graph
7. Builds and starts all application services

### 4. Start the stack (if not using setup script)

```bash
cd infra
docker compose -f docker-compose.pilot.yml up -d
```

### 5. Verify services

```bash
# All containers should show "healthy" or "running"
docker ps --format "table {{.Names}}\t{{.Status}}"

# Check Triton loaded the model
curl -s http://localhost:8001/v2/models/yolov8n | python3 -m json.tool

# Check Query API
curl -s http://localhost:8080/health
```

### 6. Open dashboards

| Service | URL | Credentials |
|---------|-----|-------------|
| Grafana | http://localhost:3000 | admin / admin |
| Query API (Swagger) | http://localhost:8080/docs | JWT required |
| MinIO Console | http://localhost:9001 | minioadmin / minioadmin123 |
| Prometheus | http://localhost:9090 | - |

In Grafana, check the **Stream Health** dashboard for camera connectivity
and the **Inference Performance** dashboard for detection latency.

### 7. Manage cameras

```bash
# Add a new camera
bash scripts/pilot/add-camera.sh \
    --id cam-5 \
    --url "rtsp://admin:pass@192.168.1.104/stream1" \
    --name "Rear Entrance"

# List all cameras with connectivity status
bash scripts/pilot/list-cameras.sh

# After adding/removing cameras, restart the edge agent
docker restart pilot-edge-agent
```

## Architecture overview

```
Camera (RTSP)
  |
  v
edge-agent --> NATS JetStream --> ingress-bridge --> Kafka
                                                      |
                              frames.sampled.refs <---+
                                                      |
                              decode-service ---------+
                                      |
                              frames.decoded.refs
                                      |
                              inference-worker (Triton YOLOv8n CPU)
                                 /        \
                    bulk.detections    tracklets.local
                         |
                    bulk-collector --> TimescaleDB
                                           |
                                      query-api --> :8080
```

## Performance expectations

On Intel i5-13500 (20 threads) with YOLOv8n ONNX on CPU:

| Metric | Expected |
|--------|----------|
| Inference latency (per frame) | 80-120 ms |
| Inference throughput | ~10 FPS |
| 4 cameras at 15% motion duty | ~3 FPS needed |
| CPU utilization (steady state) | 40-60% |
| RAM utilization | 10-13 GB |

The system is comfortable with 4 cameras. Adding more cameras or
increasing the motion duty cycle may require lowering `target_fps`
in the decode service.

## Model limitations

The pilot uses a COCO-pretrained YOLOv8n (80 classes). This differs
from the production 7-class model trained on the project taxonomy:

| Taxonomy class | COCO class index | Match |
|----------------|------------------|-------|
| person | 0 | Correct |
| bicycle | 1 | COCO index 1 is "bicycle" (correct) |
| car | 2 | COCO index 2 is "car" (correct) |
| motorcycle | 3 | COCO index 3 is "motorcycle" (correct) |
| bus | 4 | COCO index 5 is "bus" (mismatch) |
| truck | 5 | COCO index 7 is "truck" (mismatch) |
| animal | 6 | COCO has no single "animal" class |

Detections of non-taxonomy COCO classes (chair, TV, etc.) will cause
per-frame processing errors that are logged and skipped. The system
remains stable — these frames are simply not persisted.

For accurate multi-class detection, replace the COCO model with a
retrained 7-class model (see "Upgrading the model" below).

## Troubleshooting

### Camera not connecting

```bash
# Test RTSP directly
ffprobe -v quiet -rtsp_transport tcp -i "rtsp://admin:pass@IP/stream1"

# Check edge-agent logs
docker logs pilot-edge-agent --tail 50
```

Common issues:
- Wrong IP/port/credentials in RTSP URL
- Camera firewall blocking port 554
- Camera has max connection limit reached

### Triton not loading model

```bash
# Check model repository contents
ls -la infra/triton/model-repo/yolov8n/1/

# Check Triton logs
docker logs pilot-triton --tail 50
```

The ONNX file must be at `infra/triton/model-repo/yolov8n/1/model.onnx`.
Re-run the export if missing:

```bash
python3 scripts/pilot/export_yolov8n_onnx.py
```

### Kafka consumer lag

```bash
# Check lag from within the Kafka container
docker exec pilot-kafka /opt/bitnami/kafka/bin/kafka-consumer-groups.sh \
    --bootstrap-server localhost:9092 --describe --group detector-worker
```

High lag means the inference worker can't keep up. Remedies:
- Lower `DECODE__SAMPLER__TARGET_FPS` (default 5.0 → try 2.0)
- Raise `INFERENCE__DETECTOR__CONFIDENCE_THRESHOLD` (0.35 → 0.5)
- Disable cameras that are not needed

### Database migration issues

```bash
# Connect to database
docker exec -it pilot-timescaledb psql -U cilex -d vidanalytics

# Check if tables exist
\dt

# Re-run migrations
cd services/db
DATABASE_URL="postgresql+asyncpg://cilex:cilex_dev_password@localhost:5432/vidanalytics" \
    alembic upgrade head
```

## Upgrading the model

To switch from YOLOv8n COCO to a retrained 7-class model:

1. Train using ultralytics on the 7 taxonomy classes
2. Export to ONNX: `model.export(format="onnx", imgsz=640, dynamic=True)`
3. Place at `infra/triton/model-repo/yolov8n/1/model.onnx`
4. Update `config.pbtxt` output dims to `[11, 8400]` (4 + 7 classes)
5. Set `INFERENCE__DETECTOR__NUM_CLASSES=7` in `.env`
6. Restart Triton and inference worker:
   ```bash
   docker restart pilot-triton pilot-inference-worker
   ```

## Upgrading to GPU

To move from CPU to GPU inference:

1. Install [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html)
2. Replace yolov8n with yolov8l TensorRT engine
3. Update `config.pbtxt`: change `platform` to `tensorrt_plan`, `instance_group` to `KIND_GPU`
4. Add to the Triton service in docker-compose:
   ```yaml
   deploy:
     resources:
       reservations:
         devices:
           - driver: nvidia
             count: 1
             capabilities: [gpu]
   ```
5. Increase Triton memory limit (4-8 GB for GPU models)

## Stopping the pilot

```bash
cd infra
docker compose -f docker-compose.pilot.yml down

# To also remove data volumes:
docker compose -f docker-compose.pilot.yml down -v
rm -rf pilot-data/
```
