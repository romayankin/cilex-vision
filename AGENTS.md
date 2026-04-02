# Multi-Camera Video Analytics Platform

## Quick Context
Python monorepo. Services in /services. Protobuf in /proto. Infra in /infra.
Kafka + NATS + TimescaleDB + PostgreSQL + Triton + GStreamer.

## Build & Test
make test          # Run all tests
make lint          # Run ruff + mypy
make proto-check   # Validate protobuf compatibility
make up            # Start local dev stack (docker-compose)
make down          # Stop local dev stack
make migrate       # Run Alembic migrations
make seed          # Seed sample data

## Coding Rules
- Python 3.11+, type hints required
- asyncpg COPY for bulk DB writes (never row-by-row INSERT)
- Protobuf for all inter-service messages (see /proto/)
- No image bytes on Kafka — only URI references
- Every service needs: Dockerfile, tests, Prometheus metrics
- Config via Pydantic Settings loaded from YAML
- Three timestamps on every message
