# Edge Agent

Captures frames from RTSP cameras, filters by motion, and publishes
`FrameRef` protobuf messages to NATS JetStream.

## Architecture

- **GStreamer** decodes RTSP streams (H.264/H.265 via `decodebin`)
- **Motion detector** filters ~85% of static frames (configurable)
- Passing frames are JPEG-encoded and uploaded to **MinIO**
- `FrameRef` protobuf with three timestamps is published to **NATS JetStream**
- **Local ring buffer** (10 GB default) stores messages during NATS outages

## Running

```bash
# Local development
pip install -r requirements.txt
bash gen_proto.sh
EDGE_CONFIG=config.yaml python main.py

# Docker (from repo root)
docker build -f services/edge-agent/Dockerfile -t edge-agent .
docker run -v /path/to/config.yaml:/app/config.yaml edge-agent
```

## Configuration

Create a `config.yaml` (see `config.py` for all options):

```yaml
site_id: site-a
cameras:
  - camera_id: cam-lobby-01
    rtsp_url: rtsp://192.168.1.10:554/stream
  - camera_id: cam-entrance-01
    rtsp_url: rtsp://192.168.1.11:554/stream
nats:
  url: tls://nats.edge.internal:4222
  tls:
    cert_file: /etc/edge/certs/edge.crt
    key_file: /etc/edge/certs/edge.key
    ca_file: /etc/edge/certs/ca.crt
minio:
  endpoint: minio.core.internal:9000
  access_key: edge-upload
  secret_key: changeme
  bucket: frame-blobs
motion:
  pixel_threshold: 25
  motion_threshold: 0.02
  scene_change_threshold: 0.80
buffer:
  max_bytes: 10737418240
  path: /var/lib/edge-agent/buffer
metrics_port: 9090
```

## Metrics

Exposed at `GET :9090/metrics`.

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `edge_camera_uptime_ratio` | Gauge | camera_id | Per-camera uptime (0-1) |
| `edge_decode_errors_total` | Counter | camera_id | GStreamer decoder errors |
| `edge_motion_frames_total` | Counter | camera_id | Frames passing motion filter |
| `edge_static_frames_filtered_total` | Counter | camera_id | Frames filtered (no motion) |
| `edge_nats_publish_latency_ms` | Histogram | camera_id | NATS publish latency |
| `edge_buffer_fill_bytes` | Gauge | — | Local buffer usage |

## Tests

```bash
pip install pytest pytest-asyncio
pytest services/edge-agent/tests/ -v
```
