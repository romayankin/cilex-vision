# Rolling Summary

*Auto-generated after each task. Last updated: 2026-04-10 18:30 (after P2-E01)*

## Current Goal

Complete Intelligence Layer (Phase 2) — 12/16 tasks done. Overall progress: 47/70 tasks complete across all phases.

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
- MLflow tags follow the existing bake-off pattern:
- YAML frontmatter on every document (version, status, created_by, date) matching runbook pattern
- Loading/error/content rendering pattern from P2-V05

## Open Issues

- Track detail endpoint (`GET /tracks/{id}`) returns `thumbnail_url: null` — needs a frame-reference lookup table or stored thumbnail URI in `local_tracks` to resolve
- Events endpoint does not expose a signed thumbnail URL — `P2-V04` stores `thumbnail_uri` in `events.metadata_jsonb`, but `services/query-api/routers/events.py` only signs `clip_uri`
- Debug trace query endpoint lists MinIO objects directly (no DB index) — acceptable for pilot but will need a metadata table at scale
- BoT-SORT is not implemented in the repo — only ByteTrack exists in `services/inference-worker/tracker.py`. Need to implement or integrate BoT-SORT before promoting it to production
- Live tracker bake-off still needed — proxy uses MOT17 private detections, not YOLOv8-L on pilot clips. Re-validate recommendation once `data/eval/mot/` is populated

## Next Steps

23 task(s) ready to launch. Priority:
- **P2-E02** (MTMC Evaluation) → codex-cli
- **P2-O02** (Storage Tiering) → codex-cli
- **P2-O03** (Calibration Scheduler) → codex-cli
- **P2-X01** (API Documentation) → codex-cli
- **P3-V04** (Adaptive Transit Time) → claude-code
- **P3-E01** (Retraining Validation) → codex-cli

