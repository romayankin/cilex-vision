# Rolling Summary

*Auto-generated after each task. Last updated: 2026-04-11 05:00 (after P4-V05)*

## Current Goal

Complete Scale & Productization (Phase 4) — 5/12 tasks done. Overall progress: 63/70 tasks complete across all phases.

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
- Same `"use client"` + `useEffect` + `useCallback` data fetching pattern as search/admin pages
- Same loading/error/content rendering pattern
- Same Tailwind class patterns (card borders, table styling, badges, buttons)
- Same pagination button pattern from search page

## Open Issues

- Track detail endpoint (`GET /tracks/{id}`) returns `thumbnail_url: null` — needs a frame-reference lookup table or stored thumbnail URI in `local_tracks` to resolve
- Events endpoint does not expose a signed thumbnail URL — `P2-V04` stores `thumbnail_uri` in `events.metadata_jsonb`, but `services/query-api/routers/events.py` only signs `clip_uri`
- Query API still has no token issuance / login endpoint, so the new API docs rely on an external auth plane or locally minted JWTs for manual curl/Postman usage
- Query API still has no dedicated MTMC journey or `global_track_links` endpoint, so `docs/api/examples/get-journey.py` can only combine track detail, related events, and optional topology context instead of returning a true cross-camera journey
- `/topology/*` and `/debug/*` are role-gated but not filtered by `camera_scope`; confirm whether that is the intended security model before production exposure

## Next Steps

7 task(s) ready to launch. Priority:
- **P4-E01** (50-100 Camera Load Test) → codex-cli
- **P4-E02** (Zone Benchmark) → codex-cli
- **P4-O01** (Multi-Site Infrastructure) → codex-cli
- **P4-O02** (Disaster Recovery) → codex-cli
- **P4-A01** (Multi-Site Dataset) → claude-code
- **P4-X01** (Commercial Documentation) → codex-cli

