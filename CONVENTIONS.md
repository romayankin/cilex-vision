# Project Conventions — Cilex Vision
# Both CLAUDE.md and AGENTS.md point here. Both tools read this file.
# UPDATE THIS FILE after each phase or when patterns are established.
# Last updated: Phase 0 (in progress)

## Architecture

- Monorepo: services in /services, infrastructure in /infra, protobuf in /proto
- Python 3.11+ for all services
- FastAPI for HTTP APIs, asyncpg for PostgreSQL
- Kafka (central bus), NATS JetStream (edge bus)
- TimescaleDB for time-series, PostgreSQL for relational
- Triton Inference Server for model serving, GStreamer for video decode
- Protobuf for all inter-service messages, buf for linting

## Established Patterns (from completed tasks)

### Enums
- Use Python `str, enum.Enum` (not native PostgreSQL ENUM types)
- Store as TEXT columns with CHECK constraints
- See services/db/models.py for canonical examples: ObjectClass, EventType, ColorValue

### UUIDs
- Use `gen_random_uuid()` server default (not Python-side UUID generation)
- Type: `PG_UUID(as_uuid=True)` in SQLAlchemy

### Timestamps
- THREE timestamps on every message: source_capture_ts, edge_receive_ts, core_ingest_ts
- edge_receive_ts is PRIMARY for cross-camera ordering and MTMC
- Use `TIMESTAMP(timezone=True)` (TSTZ) for all timestamp columns
- See docs/time-sync-policy.md for full semantics

### Database Access
- SQLAlchemy 2.0 async models with DeclarativeBase
- Use asyncpg COPY protocol for bulk writes (copy_records_to_table), NEVER row-by-row INSERT
- Alembic for migrations with async engine
- TimescaleDB hypertables for high-volume append-only data (detections, track_observations)

### Kafka
- Topic names: see infra/kafka/topics.yaml (canonical list)
- Message keys: compound keys like {site_id}:{camera_id}:{capture_ts}:{frame_seq}
- NO image/video bytes on Kafka — only URI references to MinIO
- Protobuf serialization only (no JSON on Kafka)
- Schema validation via Confluent Schema Registry

### Protobuf
- Package structure: vidanalytics.v1.<domain>/<entity>.proto
- proto3 syntax
- snake_case for field names, UPPER_SNAKE_CASE for enum values
- buf lint must pass (STANDARD category)
- Backward compatibility enforced via buf breaking

### Service Structure
Every Python service follows this layout:
```
services/{name}/
├── main.py           # Entry point
├── config.py         # Pydantic BaseSettings, loaded from YAML
├── Dockerfile        # python:3.11-slim base
├── requirements.txt  # Pinned dependencies
├── tests/            # pytest
│   ├── __init__.py
│   ├── conftest.py   # Shared fixtures
│   └── test_*.py
└── README.md         # What it does, how to run
```

### Configuration
- Pydantic BaseSettings (v2+) loaded from YAML
- Environment variable overrides supported
- No hardcoded values for hosts, ports, credentials
- See existing config.py files for the pattern

### Prometheus Metrics
- Every service exposes GET /metrics
- Use prometheus-client library
- Naming: {service}_{metric_name}_{unit} (e.g., inference_latency_ms)
- Counter, Histogram, Gauge as appropriate

### Testing
- pytest with async support (pytest-asyncio)
- Fixtures in conftest.py
- Test categories: unit (mock dependencies), integration (real DB/Kafka in docker-compose)
- Minimum: test happy path, error paths, edge cases

### Error Handling
- Structured JSON logging via Python stdlib logging
- Never silently swallow exceptions
- Retry with exponential backoff for transient failures (network, Kafka, NATS)
- Fail fast on configuration errors (missing required config = exit, don't guess)

### Security
- mTLS for edge-to-center (see docs/security-design.md when complete)
- JWT for API auth with RBAC
- Audit logging on every data access (who, when, what)
- No hardcoded credentials anywhere

## Key Files to Read Before Writing Code

| What you need | Read this file |
|---------------|---------------|
| Object classes, attributes, events | docs/taxonomy.md |
| Kafka topics and semantics | docs/kafka-contract.md |
| Database schema and models | services/db/models.py |
| Protobuf message types | proto/vidanalytics/v1/*/*.proto |
| Timestamp rules | docs/time-sync-policy.md |
| Security requirements | docs/security-design.md |
| All architecture decisions | docs/adr/*.md |
