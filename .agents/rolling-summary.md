# Rolling Summary

*Auto-generated after each task. Last updated: 2026-04-09 20:01 (after P2-O01)*

## Current Goal

Complete Intelligence Layer (Phase 2) — 8/16 tasks done. Overall progress: 37/70 tasks complete across all phases.

## Active Constraints

- No image bytes on Kafka — only URI references to MinIO.
- asyncpg COPY protocol for all bulk DB writes, never row-by-row INSERT.
- Three timestamps on every message: source_capture_ts, edge_receive_ts (primary), core_ingest_ts.
- Embedding version boundaries — MTMC never compares across model versions.
- Triton EXPLICIT mode — shadow deploy before cutover.
- Protobuf for all inter-service messages, buf lint in CI.
- Python str enums as TEXT with CHECK constraints, not native PG ENUMs.
- If a camera has motion that never results in a tracklet, the event engine will not see it.

## Key Decisions

- ByteTrack selected as tracker (proxy bake-off on MOT17, live re-validation pending).
- FAISS flat index for real-time MTMC (30-min horizon), pgvector for historical (90 days).
- CPU-only pilot: YOLOv8n ONNX on Triton, 4 cameras, single Ubuntu node.
- This matches the repo’s existing MinIO usage pattern from attribute-service, decode-service, and MTMC checkpointing.
- **`cameras.config_json` polygon shape is still a convention, not a formal contract**

## Open Issues

- Track detail endpoint (`GET /tracks/{id}`) returns `thumbnail_url: null` — needs a frame-reference lookup table or stored thumbnail URI in `local_tracks` to resolve
- Events endpoint does not expose a signed thumbnail URL — `P2-V04` stores `thumbnail_uri` in `events.metadata_jsonb`, but `services/query-api/routers/events.py` only signs `clip_uri`
- Debug trace query endpoint lists MinIO objects directly (no DB index) — acceptable for pilot but will need a metadata table at scale
- BoT-SORT is not implemented in the repo — only ByteTrack exists in `services/inference-worker/tracker.py`. Need to implement or integrate BoT-SORT before promoting it to production
- Live tracker bake-off still needed — proxy uses MOT17 private detections, not YOLOv8-L on pilot clips. Re-validate recommendation once `data/eval/mot/` is populated

## Next Steps

20 task(s) ready to launch. Priority:
- **P2-E03** (End-to-End Stress Test) → codex-cli — unblocks 2 tasks
- **P2-A01** (Cross-Camera Annotation) → claude-code — unblocks 2 tasks
- **P3-O01** (Deployment Automation) → codex-cli — unblocks 2 tasks
- **P2-X02** (Operations Runbooks) → codex-cli — unblocks 1 tasks
- **P3-V01** (Model Retraining Pipeline) → claude-code — unblocks 1 tasks
- **P3-V02** (Shadow Deploy Tooling) → codex-cli — unblocks 1 tasks

