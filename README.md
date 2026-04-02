# Multi-Camera Video Analytics Platform

A distributed multi-camera video understanding platform that performs edge-aware acquisition, durable event-driven transport, centralized GPU inference, single-camera detection and tracking, cross-camera Re-ID and global track association, quality-gated attribute extraction, event generation, high-throughput time-series storage, and tiered clip/archive retention.

## Operating Model

**All code is written by AI coding agents (Claude Code + Codex CLI).** Humans serve as architects, reviewers, and decision-makers.

## Architecture

```
Camera / NVR / RTSP
  → Edge Agent (motion gating, local buffer)
  → NATS JetStream (edge transport)
  → Ingress Bridge (NATS→Kafka, schema validation)
  → Kafka (central data plane)
  → Decode + Inference Workers (GStreamer + Triton)
  → Single-Camera Tracker → Attribute Service → MTMC Re-ID
  → Event Engine → Clip Extraction
  → TimescaleDB + PostgreSQL + MinIO
  → FastAPI Query API + Next.js Search UI
```

## Quick Start

```bash
# Start local development stack
make up

# Run tests
make test

# Check agent task status
.agents/status.sh

# Launch an agent on a task
.agents/launch.sh P0-D01
```

## Agent System

See `.agents/` directory for the complete AI agent orchestration system:
- `.agents/roles/` — 6 role-specialized configurations (Design, Dev, Ops, Eval, Data, Doc)
- `.agents/prompts/` — per-task agent prompts (paste into Claude Code or Codex CLI)
- `.agents/manifest.yaml` — task queue with dependencies and status tracking
- `.agents/launch.sh` — agent launcher with dependency checking

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Edge transport | NATS JetStream |
| Central bus | Kafka / Redpanda |
| Inference | Triton Inference Server + TensorRT |
| Video decode | GStreamer |
| Detection | YOLO / RT-DETR (selected by bake-off) |
| Tracking | ByteTrack / BoT-SORT |
| Re-ID | OSNet + FAISS |
| Time-series DB | TimescaleDB |
| Relational DB | PostgreSQL + pgvector |
| Object storage | MinIO / S3 |
| API | FastAPI |
| Frontend | Next.js + Tailwind |
| ML Ops | MLflow + DVC + CVAT |
| Monitoring | Prometheus + Grafana |
| Serialization | Protobuf + Schema Registry |

## Phases

| Phase | Weeks | Goal |
|-------|-------|------|
| 0 — Foundation | 1–6 | Design specs, schemas, contracts, infra scaffold |
| 1 — Core Pipeline | 4–16 | Edge→detect→track→store→query on 1–4 cameras |
| 2 — Intelligence | 14–26 | Attributes, MTMC Re-ID, events, clips, UI |
| 3 — Maturity | 24–36 | Retraining, model rollout, admin UI, deploy automation |
| 4 — Scale | 34–44+ | Multi-site, 50–100 cameras, commercial packaging |

## License

Proprietary. All rights reserved.
