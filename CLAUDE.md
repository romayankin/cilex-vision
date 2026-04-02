# Project: Multi-Camera Video Analytics Platform

## Architecture
- Monorepo with services in /services, infrastructure in /infra, protobuf in /proto
- Python 3.11+ for all services, FastAPI for APIs, asyncpg for DB
- Kafka (central bus), NATS JetStream (edge), TimescaleDB + PostgreSQL
- Triton Inference Server for model serving, GStreamer for video decode
- Protobuf for all inter-service messages, Schema Registry for compatibility

## Conventions
- Use Protobuf schemas from /proto for all inter-service messages
- Use SQLAlchemy async models from /services/db/models.py
- All services must expose Prometheus metrics at /metrics
- Use Pydantic for configuration (settings loaded from YAML)
- Tests use pytest with fixtures in /tests/conftest.py
- Docker images use python:3.11-slim base

## Key Files
- docs/taxonomy.md — object classes, attributes, events, NFRs
- docs/kafka-contract.md — all Kafka topics and their semantics
- docs/security-design.md — mTLS, ACLs, trust model
- docs/adr/ — Architecture Decision Records
- .agents/manifest.yaml — task queue and dependency tracking
- .agents/roles/ — role-specific agent configurations

## Rules
- Never put image/video bytes on Kafka — only references (URIs)
- All DB writes use COPY protocol via asyncpg, never row-by-row INSERT
- Every service must have a Dockerfile and tests
- Protobuf backward compatibility enforced — no breaking changes
- Three timestamps on every message: source_capture_ts, edge_receive_ts, core_ingest_ts
