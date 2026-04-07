# Decode Service

Central Decode & Frame Sampling Service for the Cilex Vision pipeline.

## What it does

1. Consumes `FrameRef` messages from `frames.sampled.refs`
2. Downloads encoded frames from MinIO (`frame-blobs` bucket)
3. Decodes to RGB (GStreamer for H.264/H.265, Pillow for JPEG)
4. Applies BT.601/BT.709 color space normalization
5. Resizes to inference resolution (default 1280x720)
6. FPS-based sampling (default 5 FPS per camera)
7. Re-encodes as JPEG, uploads to `decoded-frames` bucket
8. Publishes updated `FrameRef` to `frames.decoded.refs`

## Configuration

Environment variables with `DECODE_` prefix override YAML config.

| Variable | Default | Description |
|---|---|---|
| `DECODE_KAFKA__BOOTSTRAP_SERVERS` | `localhost:9092` | Kafka brokers |
| `DECODE_DECODE__OUTPUT_WIDTH` | `1280` | Target frame width |
| `DECODE_DECODE__OUTPUT_HEIGHT` | `720` | Target frame height |
| `DECODE_DECODE__JPEG_QUALITY` | `90` | Output JPEG quality |
| `DECODE_DECODE__DEFAULT_COLOR_SPACE` | `bt601` | Fallback color space |
| `DECODE_SAMPLER__TARGET_FPS` | `5.0` | Max frames/sec per camera |

## Running

```bash
python main.py --config config.yaml
```

## Testing

```bash
cd services/decode-service
python -m pytest tests/ -v
```
