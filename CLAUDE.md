# Cilex Vision — Claude Code Context

## FIRST: Read CONVENTIONS.md
Before doing anything, read CONVENTIONS.md in this repo root. It contains
all established patterns, coding standards, and file references that have
been built up across completed tasks. Following these patterns ensures
consistency with code written by other agents (both Claude Code and Codex CLI).

## SECOND: Read your task context
If .claude-task-context.md exists, read it — it contains your role config
and task prompt combined by the launcher script.

## THIRD: Read handoff notes
Check .agents/handoff/ for recent notes from previous agents. These contain
decisions, gotchas, and patterns discovered during recent tasks that haven't
been promoted to CONVENTIONS.md yet.

## Architecture
See CONVENTIONS.md for full details. Quick summary:
- Python 3.11+, FastAPI, asyncpg, Kafka, NATS, TimescaleDB, Triton
- Protobuf for inter-service messages, buf for linting
- Three timestamps on every message (edge_receive_ts is PRIMARY)
- asyncpg COPY for bulk writes, never row-by-row INSERT

## When You Finish a Task
Before declaring done, write a handoff note:
1. Create .agents/handoff/{task-id}.md
2. Document: what you built, key decisions you made, patterns you established,
   gotchas the next agent should know about, any open questions.
3. This helps the next agent (which may be Codex CLI, not Claude Code)
   pick up context without a shared session.
