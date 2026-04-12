# Technology Decisions

This document summarizes the core architecture decisions that shape the current Cilex Vision implementation. It focuses on the three ADRs that most directly govern runtime structure, message flow, and persistence.

## ADR Summary

| ADR | Status | Date | Decision | Why it matters |
|---|---|---|---|---|
| [ADR-001](../adr/ADR-001-ingress-bridge.md) | Accepted | 2026-04-05 | Use a dual-bus architecture with NATS at the edge and Kafka in the core, bridged by `ingress-bridge` | Keeps the edge lightweight, isolates trust boundaries, and gives the core a durable streaming backbone |
| [ADR-002](../adr/ADR-002-kafka-partitioning.md) | Accepted | 2026-04-03 | Use URI-only Kafka messages with domain-specific topics and partition keys | Preserves per-camera ordering, avoids blob traffic on Kafka, and defines the core event/data contract |
| [ADR-003](../adr/ADR-003-database-schema.md) | Accepted | 2026-04-04 | Use TimescaleDB hypertables for high-volume time-series data and PostgreSQL relational tables for entity data | Enables COPY-based ingest, time-range query performance, retention automation, and clean separation of workloads |

## Decision Details

### ADR-001 — Dual-Bus Edge Architecture

**Summary**

- Edge sites use NATS JetStream because Kafka is too heavy for the edge footprint.
- The `ingress-bridge` is the controlled boundary where edge traffic enters the core domain.
- Blob offload, validation, idempotent keying, and `core_ingest_ts` stamping happen there.

**Rationale**

- edge hosts are resource-constrained
- WAN links can be unreliable
- the bridge is a better place to enforce validation and recovery behavior than every downstream service

**Current implementation impact**

- `edge-agent` publishes to NATS
- `ingress-bridge` forwards into Kafka
- spool and replay behavior live at the boundary instead of being pushed into every core consumer

### ADR-002 — Kafka Topic Design and Partitioning

**Summary**

- Kafka carries metadata and URI references only
- topic keys preserve the ordering guarantees needed by downstream consumers
- core services communicate through a small, explicit topic catalog rather than ad hoc streams

**Rationale**

- raw image/video bytes are too large and operationally risky for Kafka
- per-camera ordering is necessary for detection/tracking correctness
- per-event ordering is necessary for event lifecycle correctness

**Current implementation impact**

- MinIO is mandatory for frame and evidence storage
- `camera_id` remains the dominant ordering key in the live runtime
- the topic catalog is both an architecture contract and an operational deployment artifact

### ADR-003 — TimescaleDB + PostgreSQL Schema Strategy

**Summary**

- high-volume append-only data belongs in TimescaleDB hypertables
- relational and workflow-oriented data belongs in standard PostgreSQL tables
- asyncpg COPY is the standard bulk write path for heavy streams

**Rationale**

- detections and track observations are time-series workloads
- search, joins, topology, users, and events need relational behavior
- COPY-backed ingest avoids row-by-row write bottlenecks

**Current implementation impact**

- `bulk-collector` is optimized for COPY into the hypertables
- query workloads rely on time-range and camera-oriented indexing
- retention and compression are data-plane concerns, not ad hoc operator scripts

## Design Principles Extracted from the ADRs

| Principle | Meaning in practice |
|---|---|
| No bytes on Kafka | Binary assets go to MinIO; Kafka only carries metadata and references |
| Per-camera ordering by key | Camera-scoped lanes preserve frame and track sequence semantics |
| Dual-bus edge architecture | NATS at the edge, Kafka in the core, with an explicit boundary service |
| TimescaleDB for time-series | Detections and observations are stored as hypertables for retention and query performance |
| PostgreSQL for relationships | Events, tracks, links, topology, users, and audits use relational tables |
| asyncpg COPY for bulk writes | High-volume persistence paths should avoid row-by-row inserts |

## Implementation Drift to Watch

The ADRs remain the governing design intent, but the current runtime contains a few deliberate or transitional deviations:

| Area | Current reality |
|---|---|
| Attribute persistence | `attribute-service` writes directly to PostgreSQL instead of producing the `attributes.jobs` lane as an active runtime dependency |
| Event persistence | `event-engine` writes the `events` table directly while also publishing `events.raw` |
| Archive lane | `archive.transcode.*` topics exist, but the dedicated transcode worker is not part of the active service inventory |
| Topology deployment | `topology` exists as a service domain, but the current deployed API surface is mounted inside `query-api` |

Those are not reasons to ignore the ADRs. They are places where the implementation has become more hybrid than the original contract, and they should be treated as alignment work rather than invisible differences.
