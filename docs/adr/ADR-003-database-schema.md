---
status: P0-D04
date: "2026-04-04"
---

# ADR-003: Database Schema Design (TimescaleDB + PostgreSQL)

## Context

The Cilex Vision platform ingests high-volume detection and tracking data from multiple cameras, processes it through a Kafka-based pipeline, and persists it for querying and analysis. The database must support two distinct workloads:

1. **High-throughput append-only writes.** The Bulk Collector (P1-V05) uses the asyncpg COPY protocol to ingest detections at 5-10 FPS per camera (40+ rows/s at pilot scale, scaling to 500+ rows/s). Row-by-row INSERT is prohibited (see CLAUDE.md).

2. **Low-latency analytical queries.** The Query API (P1-V06) must return results within 500 ms at p95 (taxonomy.md NFR). Queries filter by camera, time range, object class, and event type.

Additionally:
- The MTMC Matcher links local (per-camera) tracks into global (cross-camera) identities.
- Events have a lifecycle (NEW -> ACTIVE -> CLOSED) that requires upsert semantics.
- Retention varies: 30 days for raw detections, indefinite for relational metadata per the 365-day NFR.
- All inter-service messages carry three timestamps: `source_capture_ts`, `edge_receive_ts`, `core_ingest_ts`.

## Decision

### 1. TimescaleDB for time-series data

Use TimescaleDB hypertables for `detections` and `track_observations`. These tables receive the highest write volume and are queried primarily by time range.

**Why not plain PostgreSQL range partitioning:**
- TimescaleDB provides automatic chunk management (no manual partition creation).
- Built-in `add_retention_policy()` drops old chunks without manual cron jobs.
- Transparent compression reduces storage 5-10x on older data.
- Chunk exclusion during query planning automatically skips irrelevant time ranges.
- Future use of continuous aggregates for dashboard rollups.

### 2. One-hour chunk intervals

At 4 cameras x 10 FPS = ~144,000 detections/hour per chunk. This keeps each chunk manageable in memory for compression and indexing. Smaller chunks (15 min) would increase planner overhead; larger chunks (1 day) would reduce compression and retention granularity.

### 3. Compression after 2 days

Data older than 2 days is rarely queried at full resolution. Compression achieves ~90% space savings on detection data. The 2-day lag ensures:
- Recent data remains uncompressed for fast ad-hoc queries.
- Late-arriving data from lagging Kafka consumers (NFR: consumer lag < 10,000 messages) is ingested before compression.

Compression configuration:
- `segmentby = camera_id` — queries almost always filter by camera.
- `orderby = time DESC` — most queries want recent data first.

### 4. Retention: 30 days for hypertables

Raw detections are high volume but low long-term value once aggregated into tracks and events. 30 days covers any reasonable investigation window. Relational tables (events, tracks, attributes) have no `drop_chunks` policy and persist indefinitely, satisfying the 365-day metadata retention NFR in taxonomy.md.

### 5. No foreign keys on hypertables

Hypertables omit foreign key constraints to maximize write throughput. The Bulk Collector writes via COPY protocol, which benefits from minimal constraint checking. Referential integrity is guaranteed by the pipeline: detections reference `camera_id` and `local_track_id` values that are created upstream before detection data is ingested.

### 6. Index strategy for < 500 ms p95

Primary indexes on hypertables:
- `(camera_id, time DESC)` — the dominant query pattern: "detections for camera X in time range."
- `(object_class, time DESC)` — class-filtered queries.
- `(local_track_id, time DESC)` — track-specific lookups.

TimescaleDB chunk exclusion ensures only relevant 1-hour chunks are scanned. Combined with the composite indexes, this targets sub-100ms for typical time-bounded queries.

Relational indexes target join and filter patterns:
- `local_tracks(camera_id, state)` — active tracks per camera.
- `events(camera_id, start_time DESC)` — events by camera and time.
- `events(track_id) WHERE track_id IS NOT NULL` — partial index skips camera-level events.

### 7. UUID primary keys

UUIDs avoid centralized sequence contention across distributed writers (Bulk Collector, MTMC Matcher). `gen_random_uuid()` is built into PostgreSQL 13+ with no extension required. Hypertables intentionally have no explicit primary key to avoid B-tree maintenance overhead on high-write tables.

### 8. JSONB for flexible metadata

- `events.metadata_jsonb` stores event-type-specific payload (e.g., loitering zone ID, stopped vehicle position) without schema migration for each new event type.
- `cameras.config_json` stores per-camera configuration (ROI polygons, loitering zones, sensitivity thresholds).
- `audit_logs.details_jsonb` stores action-specific context.

GIN indexing can be added later if JSONB queries become a bottleneck.

### 9. Bbox format: x/y/w/h (not x_min/y_min/x_max/y_max)

The database stores bounding boxes as `(bbox_x, bbox_y, bbox_w, bbox_h)` — origin point plus dimensions. The protobuf schema uses `(x_min, y_min, x_max, y_max)` — corner coordinates. The Bulk Collector converts between formats during ingestion. The x/y/w/h format is more compact for analytics queries (e.g., filtering by `bbox_w * bbox_h` for area).

### 10. Enum values as CHECK-constrained TEXT

Enums are stored as lowercase TEXT with CHECK constraints rather than PostgreSQL native ENUM types. This avoids the `ALTER TYPE ... ADD VALUE` migration complexity when adding new object classes or event types. CHECK constraints are modified with a standard `ALTER TABLE ... DROP/ADD CONSTRAINT`.

## Consequences

### Positive

- **Automated lifecycle management.** Retention and compression are handled by TimescaleDB policies — no cron jobs or manual maintenance.
- **Predictable storage growth.** 30-day retention + compression bounds hypertable storage. At pilot scale (4 cameras, 10 FPS): ~144K detections/hour x 720 hours (30 days) = ~104M rows before compression; ~10M effective rows after 90% compression on data older than 2 days.
- **Fast time-range queries.** Chunk exclusion + composite indexes target sub-100ms for the dominant query patterns.
- **Clean separation.** Hypertables handle high-volume append-only data; relational tables handle entity relationships with full FK enforcement.
- **Extensible events.** JSONB metadata allows new event attributes without schema migration.

### Negative

- **TimescaleDB dependency.** Requires the TimescaleDB extension on PostgreSQL — not available on all managed PostgreSQL services (e.g., some cloud providers require TimescaleDB Cloud or self-hosted).
- **No updates on compressed data.** Compressed chunks are read-only. Late corrections to old detections require decompression first.
- **No FK enforcement on hypertables.** Orphaned references are possible if upstream services have bugs. Mitigated by pipeline ordering guarantees.
- **UUID storage overhead.** UUIDs (16 bytes) are larger than sequential integers (8 bytes). Acceptable given the modest pilot scale and the distributed-write benefit.

### Risks & Mitigations

| Risk | Mitigation |
|------|------------|
| Chunk count grows with camera scaling | Monitor `timescaledb_information.chunks`; increase chunk interval if count exceeds 10,000 |
| Compression/decompression overhead on cross-join queries | Ensure time-range predicates are always present in queries; avoid full-table scans on compressed data |
| CHECK constraint mismatch after adding a new class | CI check: compare Python enum values with CHECK constraint values in migration |
| Bbox format mismatch between proto and DB | Document conversion requirement for Bulk Collector (P1-V05); add unit test |

## Related Documents

- [Taxonomy & Requirements](../taxonomy.md) — object classes, events, NFRs
- [Kafka Topic Contract](../kafka-contract.md) — data flow into the database
- [ER Diagram](../diagrams/schema.mermaid) — visual schema representation
- [ADR-002: Kafka Partitioning](ADR-002-kafka-partitioning.md) — upstream data flow decisions
