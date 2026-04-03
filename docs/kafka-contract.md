---
status: STUB — to be completed by task P0-D03
---

# Kafka Topic Contract

> **⚠️ This is a placeholder.** The full contract will be produced by a DESIGN agent executing task **P0-D03**.

## Topics (draft — not yet approved)

| Topic | Key | Partitions | Cleanup | Retention | Payload |
|-------|-----|-----------|---------|-----------|---------|
| frames.sampled.refs | camera_id | 12 | delete | 2h | FrameRef protobuf (URI only, no image bytes) |
| tracklets.local | camera_id | 12 | delete | 6h | Tracklet protobuf |
| attributes.jobs | local_track_id | 6 | delete | 2h | Attribute job protobuf |
| mtmc.active_embeddings | local_track_id | 12 | compact | ∞ | Embedding protobuf |
| events.raw | event_id | 6 | delete | 7d | Event protobuf |
| archive.transcode.requested | camera_id | 4 | delete | 24h | Transcode job |
| archive.transcode.completed | camera_id | 4 | delete | 24h | Transcode result |

**CRITICAL RULE:** No image/video bytes on Kafka — only URI references.
