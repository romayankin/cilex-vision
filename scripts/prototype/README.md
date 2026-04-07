# 1-Camera Throwaway Prototype

> **This is a DISPOSABLE prototype. It is NOT production code.**
> No Kafka, no Triton, no asyncpg, no mTLS. It exists only to prove the
> camera-to-browser loop works end-to-end in under 400 lines.

## What it does

- Connects to an RTSP camera (or webcam) via OpenCV
- Runs YOLOv8n at ~5 FPS, filtering to the 7 taxonomy classes
- Stores detections in a local SQLite file
- Serves a Flask web UI at `:5000` with:
  - Live MJPEG stream with bounding-box overlay
  - Table of the last 50 detections
  - Bar chart of detections per minute (last 30 min)

## Quick start

```bash
# Webcam (default)
pip install -r requirements.txt
python3 demo.py

# RTSP camera
CAMERA_URL="rtsp://user:pass@192.168.1.100:554/stream" python3 demo.py
```

Open http://localhost:5000 in your browser.

## Docker

```bash
docker build -t cilex-prototype .
docker run --rm -p 5000:5000 \
  -e CAMERA_URL="rtsp://user:pass@192.168.1.100:554/stream" \
  cilex-prototype
```

For webcam passthrough on Linux, add `--device /dev/video0`.

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `CAMERA_URL` | `0` (first webcam) | RTSP URL or webcam index |
| `DB_PATH` | `detections.db` | SQLite database file path |

## Taxonomy mapping

Detections are filtered to match `docs/taxonomy.md`:

| COCO class(es) | Taxonomy class |
|-----------------|---------------|
| person | person |
| car | car |
| truck | truck |
| bus | bus |
| bicycle | bicycle |
| motorcycle | motorcycle |
| bird, cat, dog, horse, sheep, cow, elephant, bear, zebra, giraffe | animal |

All other COCO classes are ignored.
