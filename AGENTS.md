# Cilex Vision — Codex CLI Context

## FIRST: Read CONVENTIONS.md
Read CONVENTIONS.md in this repo root before doing anything. It contains
all coding patterns and standards established by previous agents. Following
these ensures your code is consistent with work done by Claude Code agents.

## SECOND: Check handoff notes
Read all files in .agents/handoff/ — these are notes from the previous agent
that worked on related tasks. They contain decisions and gotchas that are
critical for consistency.

## Build & Test
make test          # Run all tests
make lint          # Run ruff + mypy
make proto-check   # Validate protobuf compatibility
make up            # Start local dev stack (docker-compose)
make down          # Stop local dev stack
make migrate       # Run Alembic migrations

## Quick Rules (details in CONVENTIONS.md)
- Python 3.11+, type hints required
- asyncpg COPY for bulk DB writes (NEVER row-by-row INSERT)
- Protobuf for all Kafka messages (no JSON)
- No image bytes on Kafka — only URI references
- Every service: Dockerfile, tests, Prometheus metrics at /metrics
- Config via Pydantic BaseSettings from YAML
- Three timestamps: source_capture_ts, edge_receive_ts, core_ingest_ts

## When You Finish
Create .agents/handoff/{task-id}.md with:
- What you built and key decisions
- Patterns you established
- Gotchas for the next agent

## First Step — Always
Read PROJECT-STATUS.md before doing anything. It contains all architecture decisions, completed tasks, component details, and current priorities.
