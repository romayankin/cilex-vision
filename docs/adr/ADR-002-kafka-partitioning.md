---
status: P0-D03
date: "2026-04-03"
---

# ADR-002: Kafka Topic Design & Partitioning Strategy

## Context

The Cilex Vision Multi-Camera Video Analytics Platform uses Kafka as its central message bus for all inter-service communication in the core pipeline. The platform processes video frames from multiple cameras through a pipeline of detection, tracking, attribute extraction, embedding computation, cross-camera matching, and event generation.

Key constraints driving this decision:

1. **No raw bytes on Kafka.** Video and image data are large (100 KB–10 MB per frame). Kafka is optimised for small messages (< 1 MB). Object storage (MinIO/S3) is purpose-built for blob storage.
2. **Per-camera ordering.** The detector and tracker require frames to arrive in capture order per camera. Out-of-order frames break temporal association.
3. **Per-event lifecycle ordering.** Events transition through states (NEW → ACTIVE → CLOSED). Out-of-order state updates corrupt event records.
4. **Compaction for active gallery.** The MTMC matcher needs a materialised view of all active track embeddings. This is a natural fit for Kafka log compaction.
5. **Pilot scale.** 4 cameras at 5–10 FPS each ≈ 20–40 messages/s on `frames.sampled.refs`. The design must handle this comfortably while allowing growth to ~50 cameras without re-architecture.

## Decision

### 1. URI-only messages

All Kafka messages carry metadata and URI references to object storage — never raw image or video bytes. Producers write blobs to MinIO/S3 before publishing the Kafka message.

### 2. Seven topics with domain-specific partition keys

| Topic | Key | Partitions | Rationale |
|-------|-----|-----------|-----------|
| `frames.sampled.refs` | `camera_id` | 12 | Per-camera frame ordering for detector/tracker |
| `tracklets.local` | `camera_id` | 12 | Per-camera track state ordering; single-writer guarantee |
| `attributes.jobs` | `local_track_id` | 6 | Group attributes per track for batch writes |
| `mtmc.active_embeddings` | `local_track_id` | 12 | Compaction key = one embedding per track |
| `events.raw` | `event_id` | 6 | Per-event lifecycle ordering; avoids hot partitions |
| `archive.transcode.requested` | `camera_id` | 4 | Serialise transcodes per camera |
| `archive.transcode.completed` | `camera_id` | 4 | Mirror request partitioning |

### 3. Partition count rationale

- **12 partitions** for high-throughput topics (`frames`, `tracklets`, `embeddings`): supports up to 12 concurrent consumers and handles ~12 cameras with uniform distribution.
- **6 partitions** for medium-throughput topics (`attributes`, `events`): sufficient for current load; events are much lower frequency than frames.
- **4 partitions** for low-throughput topics (`archive.*`): transcode is I/O-bound, not throughput-bound; limiting parallelism prevents resource contention.

### 4. Retention policies

- **Short retention (2 h):** Topics where data is consumed quickly and the authoritative copy lives in TimescaleDB or MinIO (`frames.sampled.refs`, `attributes.jobs`).
- **Medium retention (6 h, 24 h):** Topics where replay may be needed for missed processing or debugging (`tracklets.local`, `archive.*`).
- **Long retention (7 d):** Events are the primary business output; longer retention enables debugging, replay, and late consumers (`events.raw`).
- **Infinite / compacted:** The embedding gallery must persist for the lifetime of active tracks (`mtmc.active_embeddings`).

### 5. Cleanup policies

- **delete:** Default. Old segments are removed after retention expires.
- **compact:** Used only for `mtmc.active_embeddings`. The MTMC matcher materialises a key-value store from this topic; compaction keeps the log bounded while preserving the latest embedding per track.

## Consequences

### Positive

- **Clear ordering guarantees.** Per-camera keying on `frames` and `tracklets` eliminates ordering bugs in the detector and tracker.
- **Bounded storage.** Compaction on `mtmc.active_embeddings` prevents unbounded growth. Delete policies on all other topics ensure Kafka storage is predictable.
- **Simple scaling.** Adding cameras up to the partition count requires no topic changes. Beyond 12 cameras, a partition increase is straightforward (though it requires a brief drain).
- **No large messages.** URI-only messages stay well under Kafka's 1 MB default limit, avoiding broker memory pressure.

### Negative

- **Object storage dependency.** Every consumer must have network access to MinIO/S3 to retrieve frame/clip data. If MinIO is unavailable, consumers stall even though Kafka is healthy.
- **Partition count is hard to change.** Increasing partitions redistributes keys, temporarily breaking ordering for in-flight messages. This requires a coordinated drain-and-restart procedure.
- **`event_id` keying spreads events across partitions.** Consumers that need per-camera event ordering must implement client-side grouping. This was accepted as a trade-off to avoid hot partitions with 4 cameras and 6 partitions.

### Risks & Mitigations

| Risk | Mitigation |
|------|------------|
| Hot partition if cameras hash to same partition | Monitor `kafka_messages_in_per_sec` per partition; rebalance if skew > 2x |
| MinIO outage stalls pipeline | Circuit breaker in consumers; retry with exponential backoff; alert on consumer lag |
| Schema evolution breaks consumers | Schema Registry with BACKWARD compatibility; `buf breaking` in CI |
| Compaction lag on embeddings topic | Monitor `kafka_log_cleaner_recopy_percent`; tune `min.compaction.lag.ms` |
