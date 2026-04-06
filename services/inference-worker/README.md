# Inference Worker

Detection & tracking inference worker for the Cilex Vision pipeline.

## What it does

Consumes `FrameRef` messages from Kafka (`frames.sampled.refs`), runs the
full detect → track → embed pipeline, and publishes results:

- **Detections** → `bulk.detections` (for DB storage via bulk-collector)
- **Tracklets** → `tracklets.local` (per-camera track state)
- **Embeddings** → `mtmc.active_embeddings` (Re-ID, compacted topic)

## Pipeline stages

1. Download JPEG frame from MinIO (via `frame_uri`)
2. YOLOv8-L detection via Triton gRPC (letterbox 640×640, NMS)
3. ByteTrack per-camera tracking (CPU, Kalman + Hungarian)
4. OSNet Re-ID embedding via Triton gRPC (best frame per track)
5. Publish all results to Kafka
6. Debug trace sampling (1–5%, always on low-confidence)

## Configuration

```yaml
triton:
  url: "localhost:8001"
kafka:
  bootstrap_servers: "localhost:9092"
  consumer_group: "detector-worker"
minio:
  endpoint: "localhost:9000"
tracker:
  track_thresh: 0.5
  max_lost_frames: 50
detector:
  confidence_threshold: 0.40
  nms_iou_threshold: 0.45
debug:
  sample_rate_pct: 2.0
metrics_port: 9090
```

Environment overrides use the `INFERENCE_` prefix (e.g., `INFERENCE_TRITON__URL`).

## Metrics

| Metric | Type | Labels |
|--------|------|--------|
| `inference_detections_total` | Counter | `object_class` |
| `inference_tracks_active` | Gauge | `camera_id` |
| `inference_tracks_closed_total` | Counter | `camera_id` |
| `inference_latency_ms` | Histogram | — |
| `inference_embedding_latency_ms` | Histogram | — |
| `inference_frames_consumed_total` | Counter | — |
| `inference_publish_errors_total` | Counter | `topic` |
| `inference_consumer_lag` | Gauge | `topic`, `partition` |

## Running locally

```bash
# Generate protobuf code
bash gen_proto.sh

# Run tests
cd tests && python -m pytest -v

# Start service
python main.py --config config.yaml
```

## Docker

Build from repository root:

```bash
docker build -f services/inference-worker/Dockerfile -t inference-worker .
```

## Tests

```bash
python -m pytest services/inference-worker/tests/ -v
```
