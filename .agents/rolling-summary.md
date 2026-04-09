# Rolling Summary

*Auto-generated after each task. Last updated: 2026-04-09 18:19 (after P2-V01)*

## Current Goal

Complete Intelligence Layer (Phase 2) — 3/16 tasks done. Overall progress: 32/70 tasks complete across all phases.

## Active Constraints

- No image bytes on Kafka — only URI references to MinIO.
- asyncpg COPY protocol for all bulk DB writes, never row-by-row INSERT.
- Three timestamps on every message: source_capture_ts, edge_receive_ts (primary), core_ingest_ts.
- Embedding version boundaries — MTMC never compares across model versions.
- Triton EXPLICIT mode — shadow deploy before cutover.
- Protobuf for all inter-service messages, buf lint in CI.
- Python str enums as TEXT with CHECK constraints, not native PG ENUMs.

## Key Decisions

- ByteTrack selected as tracker (proxy bake-off on MOT17, live re-validation pending).
- FAISS flat index for real-time MTMC (30-min horizon), pgvector for historical (90 days).
- CPU-only pilot: YOLOv8n ONNX on Triton, 4 cameras, single Ubuntu node.
- `gen_proto.sh`, `requirements.txt`, `Dockerfile` — standard patterns.
- White balance as a thin cv2.xphoto wrapper (skip-on-IR pattern)
- `gen_proto.sh` — Proto generation following inference-worker pattern.

## Open Issues

- Track detail endpoint (`GET /tracks/{id}`) returns `thumbnail_url: null` — needs a frame-reference lookup table or stored thumbnail URI in `local_tracks` to resolve
- Debug trace query endpoint lists MinIO objects directly (no DB index) — acceptable for pilot but will need a metadata table at scale
- BoT-SORT is not implemented in the repo — only ByteTrack exists in `services/inference-worker/tracker.py`. Need to implement or integrate BoT-SORT before promoting it to production
- Live tracker bake-off still needed — proxy uses MOT17 private detections, not YOLOv8-L on pilot clips. Re-validate recommendation once `data/eval/mot/` is populated
- BoT-SORT published throughput (6.8 FPS) is ~4x slower than ByteTrack (29.6 FPS) — measure real latency on target GPU stack before committing to BoT-SORT in production

## Next Steps

18 task(s) ready to launch. Priority:
- **P2-V05** (Search UI & Timeline) → claude-code — unblocks 3 tasks
- **P2-V01** (Attribute Extraction Service) → claude-code — unblocks 2 tasks
- **P2-V03** (Event Engine) → codex-cli — unblocks 2 tasks
- **P2-A01** (Cross-Camera Annotation) → claude-code — unblocks 2 tasks
- **P3-O01** (Deployment Automation) → codex-cli — unblocks 2 tasks
- **P2-O01** (MTMC Infrastructure) → codex-cli — unblocks 1 tasks

