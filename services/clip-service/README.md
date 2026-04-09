# Clip Service

Consumes closed `events.raw` records, builds event clips from decoded JPEG frames in MinIO, generates thumbnails, uploads both assets, updates PostgreSQL, and publishes a completion record to `archive.transcode.completed`.

## Current pipeline assumptions

- Source frames come from the `decoded-frames` bucket created by the decode service.
- Decoded frame objects are currently stored as `camera_id/YYYY-MM-DD/frame_id.jpg`.
- Because those keys do not include capture timestamps and there is no persisted frame-index table yet, clip window selection filters MinIO objects by `last_modified` within the event window.

## Asset storage

- Clip URI: `s3://event-clips/{site_id}/{camera_id}/{YYYY-MM-DD}/{event_id}.mp4` when the camera has a site mapping in PostgreSQL
- Thumbnail URI: `s3://thumbnails/{site_id}/{camera_id}/{YYYY-MM-DD}/{event_id}_thumb.jpg`
- If the camera has no site mapping, keys fall back to `{camera_id}/{YYYY-MM-DD}/...`
- `thumbnail_uri` is stored in `events.metadata_jsonb`

## Validation

```bash
pytest services/clip-service/tests -q
ruff check services/clip-service
mypy --config-file services/clip-service/mypy.ini services/clip-service
docker build -f services/clip-service/Dockerfile . -t cilex-clip-service:test
```
