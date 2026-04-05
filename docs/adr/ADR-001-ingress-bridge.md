---
status: P0-D06
date: "2026-04-05"
---

# ADR-001: Ingress Bridge Service

## Context

The Cilex Vision platform uses a dual-bus architecture ([ADR in PROJECT-STATUS.md §ADR-001](#)): NATS JetStream at the edge and Apache Kafka in the core. Edge agents capture video frames, apply motion filtering (~15% pass-through), and publish `FrameRef` protobuf messages to a local NATS JetStream instance. Core pipeline services — the detector, tracker, bulk collector, and others — consume exclusively from Kafka topics defined in [docs/kafka-contract.md](../kafka-contract.md).

A dedicated bridge service is needed because:

1. **Edge cannot run Kafka.** Kafka requires 3 brokers and 2-4 GB RAM. Edge nodes are resource-constrained (single-board or NUC-class hardware). NATS JetStream runs in ~50 MB.
2. **Protocol translation is non-trivial.** NATS subjects must map to Kafka topics with correct partition keys. Blob offloading, schema validation, and idempotent keying add logic beyond a simple proxy.
3. **Reliability gap.** The WAN link between edge and core is unreliable. The bridge must spool locally during Kafka outages and drain without overwhelming the cluster on recovery.
4. **Operational boundary.** The bridge is the trust boundary where edge-side data enters the core domain. Schema validation, rate limiting, and `core_ingest_ts` stamping happen here.

### Constraints

- **No image/video bytes on Kafka** — only URI references to MinIO ([CLAUDE.md rules](../../CLAUDE.md)).
- **Three timestamps** must be fully populated before publishing to Kafka ([docs/time-sync-policy.md](../time-sync-policy.md)).
- **`core_ingest_ts`** is stamped exactly once at the first core boundary — this is the Ingress Bridge.
- **`edge_receive_ts`** is the authoritative cross-camera timestamp; the bridge MUST NOT overwrite it.
- **Protobuf serialization** enforced via Confluent Schema Registry (BACKWARD compatibility).
- **mTLS** between edge agents and NATS JetStream; TLS + SASL between bridge and Kafka ([docs/security-design.md](../security-design.md) — stub, see trust model draft).

---

## Decision

Build a dedicated **Ingress Bridge** service that consumes from NATS JetStream, validates and transforms messages, offloads blobs to MinIO, and produces to Kafka with exactly the guarantees needed by downstream consumers.

### Responsibilities

The Ingress Bridge has eight core responsibilities, executed in order for every inbound message:

#### 1. Durable NATS Consumption

- Subscribe to NATS JetStream subjects per site: `frames.>{site_id}`.
- Use a durable consumer with explicit acknowledgment (`AckPolicy: explicit`).
- Ack only after successful Kafka produce (or spool write on Kafka failure).
- If NATS is unavailable, block and retry with exponential backoff (1s → 30s cap). Do not crash.

#### 2. Protobuf Schema Validation

- Deserialize every inbound message as `vidanalytics.v1.frame.FrameRef` using the Confluent Schema Registry deserializer.
- Reject messages that fail deserialization. Rejected messages are:
  - Nack'd on NATS (redelivered up to 3 times).
  - After 3 failures, published to the NATS dead-letter subject `frames.dlq.>{site_id}` and ack'd to prevent infinite redelivery.
- Validate required fields: `frame_id`, `camera_id`, `frame_uri`, `frame_sequence`, and all three timestamp fields in `VideoTimestamp` must be non-empty/non-zero.
- Validate `frame_uri` starts with `s3://` or `minio://`. Reject URIs containing raw base64 or data URIs.

#### 3. Blob Offloading (> 100 KB)

- If any auxiliary payload attached to the NATS message exceeds 100 KB (e.g., a thumbnail or debug snapshot), upload it to MinIO bucket `frame-blobs` and replace the inline payload with a URI reference.
- The `FrameRef.frame_uri` field itself already points to MinIO (written by the edge agent). This step handles any additional metadata blobs that edge agents may attach.
- MinIO writes use `PutObject` with `Content-MD5` verification. On upload failure, retry 3 times with 1s backoff, then spool the entire message for later retry.

#### 4. Idempotent Kafka Key Construction

Every Kafka message key is a compound string:

```
{site_id}:{camera_id}:{capture_ts_epoch_us}:{frame_seq}
```

Where:
- `site_id` — extracted from the NATS subject hierarchy or message metadata.
- `camera_id` — from `FrameRef.camera_id`.
- `capture_ts_epoch_us` — `source_capture_ts` as microseconds since Unix epoch (for key uniqueness; not used for ordering).
- `frame_seq` — `FrameRef.frame_sequence` (monotonic per camera).

This key is deterministic: replaying the same NATS message produces the same Kafka key. Combined with Kafka's idempotent producer (`enable.idempotence=true`), this guarantees at-most-once delivery per key within a producer session.

The **partition key** (used for Kafka partitioning) is `camera_id` alone — matching the `frames.sampled.refs` topic contract in [docs/kafka-contract.md §3.1](../kafka-contract.md).

#### 5. Timestamp Stamping: `core_ingest_ts`

- Stamp `core_ingest_ts` in the `VideoTimestamp` field immediately before producing to Kafka.
- Use the bridge host's Chrony-disciplined clock (UTC).
- MUST NOT modify `source_capture_ts` or `edge_receive_ts` — these are write-once at the edge per [docs/time-sync-policy.md §2](../time-sync-policy.md).
- Validate: `core_ingest_ts >= edge_receive_ts`. If violated, log a warning (clock skew detected) but do not reject the message.

#### 6. NVMe Spool on Kafka Failure

When the Kafka producer fails (broker unavailable, timeout, or produce error after 3 retries):

- Write the fully prepared message (validated, blob-offloaded, key-constructed, `core_ingest_ts`-stamped) to a local NVMe spool directory as a length-prefixed protobuf file.
- Spool directory structure: `spool/{topic}/{partition_key}/{timestamp_ns}.pb`.
- **Maximum spool size: 50 GB.** When the spool reaches 50 GB:
  - Stop accepting new messages from NATS (stop ack'ing — NATS will buffer on its side up to its stream limit).
  - Emit metric `bridge_spool_full_total` and alert.
  - Resume accepting when spool drains below 40 GB (80% high-water mark).
- Spool writes are `fsync`'d to guarantee durability on unexpected power loss.
- Messages in the spool retain their original idempotent key for deduplication on replay.

#### 7. Spool Drain with Rate Limiting on Recovery

When Kafka becomes available again:

- Drain the spool in FIFO order (oldest messages first, by filename timestamp).
- **Rate limit: drain at 80% of the configured per-site rate limit** (default: 400 msg/s of the 500 msg/s site limit). This prevents the drain from consuming the full bandwidth and starving live traffic.
- Live traffic takes priority. If live ingestion rate approaches the site rate limit, pause drain and resume when headroom is available.
- Each spooled message is produced to Kafka with the same idempotent key. Kafka's idempotent producer handles any duplicates from partial sends before the outage.
- Delete spool files only after successful Kafka produce acknowledgment (`acks=all`).
- Emit metric `bridge_spool_drain_msg_total` (counter) and `bridge_spool_depth_bytes` (gauge) during drain.

#### 8. Per-Site Rate Limiting

- Default rate limit: **500 msg/s per site**.
- Configurable per site via YAML configuration.
- Rate limiting is applied after validation but before Kafka produce.
- When a site exceeds its rate limit:
  - Delay NATS ack (backpressure propagates to edge via NATS flow control).
  - Emit metric `bridge_rate_limited_total{site_id}`.
- Rate limiting does NOT apply to spool drain traffic (which has its own 80% limit).

### Live vs. Replay Lane Separation

The bridge supports two logical lanes on the same service instance:

| Lane | NATS Subject Pattern | Kafka Topic | Priority | Rate Limit |
|------|---------------------|-------------|----------|------------|
| **Live** | `frames.live.>{site_id}` | `frames.sampled.refs` | High | Per-site limit (500 msg/s default) |
| **Replay** | `frames.replay.>{site_id}` | `frames.sampled.refs` | Low | 50% of per-site limit |

- Live traffic always takes priority. When the combined rate approaches the site limit, replay is paused.
- Replay messages carry a NATS header `X-Replay: true` so the bridge can route them to the low-priority lane.
- Both lanes write to the same Kafka topic (`frames.sampled.refs`) with the same key format — downstream consumers are lane-agnostic.
- Replay messages have `source_capture_ts` in the past (potentially days old). The bridge stamps `core_ingest_ts` at the current time. Downstream services use `edge_receive_ts` for ordering (per time-sync policy).

---

## Delivery Guarantee

**At-least-once from edge to Kafka.**

| Guarantee | Mechanism |
|-----------|-----------|
| NATS → Bridge | Durable consumer with explicit ack. Message redelivered if not ack'd within 30s. |
| Bridge → Kafka | Idempotent producer (`enable.idempotence=true`, `acks=all`). Retries on transient failure. |
| Bridge → Spool → Kafka | Spooled messages retain idempotent key. Replay produces are deduplicated by Kafka. |
| End-to-end dedup | Downstream consumers (Bulk Collector) use `ON CONFLICT (idempotent_key) DO NOTHING` upsert. |

The bridge does NOT provide exactly-once semantics. Exactly-once would require Kafka transactions coordinated with NATS ack — adding latency and complexity disproportionate to the benefit. At-least-once with downstream deduplication by key is sufficient.

---

## Failure Modes

| Failure | Detection | Behavior | Recovery |
|---------|-----------|----------|----------|
| **NATS unavailable** | Connection error, health check fails | Block and retry with exponential backoff (1s → 30s). No messages processed. Emit `bridge_nats_disconnected` metric. | Auto-reconnect on NATS recovery. NATS JetStream replays unack'd messages. |
| **Kafka unavailable** | Produce timeout (30s), broker connection error | Spool to NVMe. Continue consuming from NATS. Emit `bridge_kafka_unavailable` metric. | Auto-detect Kafka recovery (periodic 5s health probe). Drain spool at 80% rate limit. |
| **Kafka partial failure** (one broker down) | Produce errors on specific partitions | Kafka client handles leader re-election. Retries (3x, 1s backoff) succeed once new leader elected. | Transparent — handled by Kafka client. |
| **MinIO unavailable** | `PutObject` error after 3 retries | Spool the entire message. Do not produce to Kafka without blob offload. Emit `bridge_minio_unavailable` metric. | Drain spool retries MinIO upload before Kafka produce. |
| **Schema validation failure** | Deserialization error | Nack on NATS (up to 3 redeliveries). After 3 failures, publish to `frames.dlq.>{site_id}` and ack. Emit `bridge_schema_reject_total`. | Manual inspection of DLQ. Fix source (edge agent bug). |
| **Spool full (50 GB)** | `bridge_spool_depth_bytes >= 50GB` | Stop ack'ing NATS messages. NATS buffers on its side. Emit `bridge_spool_full_total` alert. | Resume when spool drains below 40 GB. Alert triggers ops investigation of prolonged Kafka outage. |
| **Bridge crash** | Process exit, health check fails | Kubernetes restarts pod. Unack'd NATS messages are redelivered. Spool survives on persistent volume. | On restart: resume spool drain (if any), reconnect NATS and Kafka. |
| **Clock drift** | `core_ingest_ts < edge_receive_ts` | Log warning, produce message anyway (do not reject). Emit `bridge_clock_drift_detected` metric. | Ops investigates Chrony status on bridge host. |
| **Rate limit exceeded** | `bridge_rate_limited_total` counter incrementing | Delay NATS ack, backpressure propagates to edge. | Self-recovering once source rate decreases. Review rate limit config if sustained. |
| **Corrupt spool file** | Protobuf deserialization error during drain | Move corrupt file to `spool/quarantine/`. Log error. Continue draining remaining files. Emit `bridge_spool_corrupt_total`. | Manual inspection of quarantined files. |

---

## Health, Readiness & Metrics

### Health Endpoints

| Endpoint | Purpose | Returns 200 when |
|----------|---------|------------------|
| `GET /health` | Liveness probe | Process is running and main event loop is responsive |
| `GET /ready` | Readiness probe | NATS connected AND Kafka producer initialized AND MinIO reachable |
| `GET /metrics` | Prometheus scrape | Always (metrics are always available) |

### Prometheus Metrics

All metrics use the prefix `bridge_`.

#### Counters

| Metric | Labels | Description |
|--------|--------|-------------|
| `bridge_messages_received_total` | `site_id`, `lane` | Messages received from NATS (live or replay) |
| `bridge_messages_produced_total` | `site_id`, `topic` | Messages successfully produced to Kafka |
| `bridge_messages_spooled_total` | `site_id` | Messages written to NVMe spool |
| `bridge_spool_drain_msg_total` | `site_id` | Spooled messages successfully drained to Kafka |
| `bridge_schema_reject_total` | `site_id`, `reason` | Messages rejected by schema validation |
| `bridge_rate_limited_total` | `site_id` | Messages delayed by rate limiter |
| `bridge_blob_offload_total` | `site_id`, `bucket` | Blobs uploaded to MinIO |
| `bridge_nats_reconnect_total` | | NATS reconnection events |
| `bridge_spool_full_total` | | Spool full events (acceptance paused) |
| `bridge_spool_corrupt_total` | | Corrupt spool files quarantined |
| `bridge_clock_drift_detected` | | `core_ingest_ts < edge_receive_ts` events |
| `bridge_dlq_published_total` | `site_id` | Messages published to NATS DLQ after 3 failures |

#### Histograms

| Metric | Labels | Buckets (ms) | Description |
|--------|--------|--------------|-------------|
| `bridge_nats_to_kafka_latency_ms` | `site_id` | 1, 5, 10, 25, 50, 100, 250, 500, 1000 | Time from NATS receive to Kafka ack |
| `bridge_blob_offload_latency_ms` | `bucket` | 10, 50, 100, 250, 500, 1000, 5000 | MinIO upload duration |
| `bridge_produce_latency_ms` | `topic` | 1, 5, 10, 25, 50, 100, 250 | Kafka produce call duration |

#### Gauges

| Metric | Labels | Description |
|--------|--------|-------------|
| `bridge_spool_depth_bytes` | | Current spool size on disk |
| `bridge_spool_depth_messages` | | Current number of spooled messages |
| `bridge_nats_consumer_pending` | `site_id` | Pending (unack'd) messages in NATS consumer |
| `bridge_kafka_producer_inflight` | | In-flight Kafka produce requests |
| `bridge_rate_limit_headroom_pct` | `site_id` | Percentage of rate limit capacity remaining |

---

## Deployment

### Resource Requirements

| Resource | Minimum | Recommended | Notes |
|----------|---------|-------------|-------|
| **Instances** | 2 | 2 | Active-active behind NATS queue group for HA. Both instances consume from the same NATS durable consumer. |
| **vCPU** | 4 | 4-8 | CPU-bound on protobuf serialization and Kafka produce. Scale vCPU with camera count. |
| **RAM** | 16 GB | 16-32 GB | Kafka producer buffers (~128 MB per instance), NATS client buffers, protobuf arena. |
| **NVMe spool** | 500 GB | 500 GB - 1 TB | Local NVMe for spool. Size for 12-24 hours of Kafka outage at peak ingestion rate. |
| **Network** | 1 Gbps | 10 Gbps | Sufficient for 4-camera pilot. 10 Gbps for 50+ cameras with blob offload traffic. |

### Scaling Model

At pilot scale (4 cameras, 10 FPS, ~15% edge pass-through = ~6 msg/s per camera = ~24 msg/s total), a single bridge instance handles the load with > 95% headroom. Two instances are deployed for high availability, not throughput.

Scaling triggers:
- **Add instances** when `bridge_rate_limit_headroom_pct` consistently < 20% across all instances.
- **Increase spool** when Kafka outages lasting > 6 hours are expected.
- **Increase vCPU** when `bridge_nats_to_kafka_latency_ms` p99 exceeds 100 ms under normal (non-outage) conditions.

### Deployment Configuration

```yaml
# Pydantic BaseSettings (config.py)
bridge:
  nats_url: "nats://nats.internal:4222"
  nats_credentials_file: "/etc/nats/bridge.creds"
  nats_durable_name: "ingress-bridge"

  kafka_bootstrap_servers: "kafka-0:9092,kafka-1:9092,kafka-2:9092"
  kafka_security_protocol: "SASL_SSL"
  kafka_sasl_mechanism: "SCRAM-SHA-256"
  kafka_producer_acks: "all"
  kafka_producer_enable_idempotence: true
  kafka_producer_compression: "zstd"
  kafka_producer_linger_ms: 5
  kafka_producer_batch_size: 65536

  minio_endpoint: "minio.internal:9000"
  minio_access_key: "${MINIO_ACCESS_KEY}"
  minio_secret_key: "${MINIO_SECRET_KEY}"
  minio_bucket_blobs: "frame-blobs"

  schema_registry_url: "http://schema-registry.internal:8081"

  spool_dir: "/data/spool"
  spool_max_bytes: 53687091200  # 50 GB
  spool_resume_pct: 80          # resume at 80% = 40 GB

  rate_limit_default_msg_per_sec: 500
  rate_limit_replay_pct: 50     # replay lane gets 50% of site limit
  spool_drain_pct: 80           # drain at 80% of site limit

  sites:
    - site_id: "site-alpha"
      nats_subject: "frames.live.site-alpha"
      rate_limit_msg_per_sec: 500  # override per site
```

### Container

```dockerfile
FROM python:3.11-slim
# ... standard service layout per CONVENTIONS.md
```

Persistent volume mount: `/data/spool` on local NVMe (not network storage — latency-sensitive).

---

## Consequences

### Positive

- **Clean protocol boundary.** Edge agents only need to speak NATS. Core services only need to speak Kafka. Neither side knows about the other's protocol.
- **Resilient to Kafka outages.** The NVMe spool absorbs up to 50 GB (~12-24 hours at pilot rate) of messages during Kafka downtime. Edge agents are unaffected.
- **Idempotent replay.** The deterministic key format ensures that replayed messages (from spool or NATS redelivery) are deduplicated by Kafka and downstream consumers.
- **Observable.** 12 counters, 3 histograms, and 5 gauges provide complete visibility into bridge health, throughput, and failure modes.
- **Rate-controlled recovery.** Spool drain at 80% rate limit prevents the post-outage "thundering herd" from overwhelming Kafka or downstream consumers.

### Negative

- **Additional service to operate.** The bridge is a stateful service (NVMe spool) that requires persistent volume management and monitoring.
- **Added latency.** The bridge adds 5-50 ms to the edge-to-Kafka path (protobuf validation, MinIO upload if needed, Kafka produce). Acceptable given the 2000 ms e2e latency budget.
- **Single-point for `core_ingest_ts`.** If the bridge's Chrony drifts, all `core_ingest_ts` values are skewed. Mitigated by clock drift monitoring ([docs/time-sync-policy.md §5](../time-sync-policy.md)).
- **Spool is per-instance.** If an instance crashes, its spool is orphaned until restart. Not replicated across instances. Mitigated by persistent volumes and Kubernetes restart policy.

### Risks & Mitigations

| Risk | Mitigation |
|------|------------|
| NVMe failure loses spooled messages | RAID-1 or replicated persistent volume. Alert on disk health. NATS retains unack'd messages as fallback. |
| Spool drain overwhelms Kafka on recovery | Rate-limited drain (80%). Live traffic priority. Gradual ramp-up. |
| Schema Registry unavailable blocks all traffic | Cache last-known schema locally. Produce with cached schema. Alert on registry unavailability. |
| Edge floods bridge beyond rate limit | Per-site rate limiting. NATS backpressure (delayed ack). Edge agent respects NATS flow control. |
| Replay lane starves during sustained live load | Replay is explicitly lower priority. If replay SLA matters, increase the site rate limit or add bridge instances. |

---

## Related Documents

- [Kafka Topic Contract](../kafka-contract.md) — downstream topic definitions, partition keys, consumer groups
- [Time Synchronization Policy](../time-sync-policy.md) — `core_ingest_ts` stamping rules
- [Security Design](../security-design.md) — mTLS, SASL, trust model (stub — P0-D08)
- [FrameRef Proto](../../proto/vidanalytics/v1/frame/frame.proto) — message schema
- [VideoTimestamp Proto](../../proto/vidanalytics/v1/common/common.proto) — three-timestamp model
- [Ingress Bridge Flow Diagram](../diagrams/ingress-bridge-flow.mermaid) — visual flow for normal, outage, and replay
- [ADR-002: Kafka Partitioning](ADR-002-kafka-partitioning.md) — topic design rationale

---

## Acceptance Criteria

### Automated (checked by `review.sh`)

- [ ] File `docs/adr/ADR-001-ingress-bridge.md` exists and is > 100 lines
- [ ] YAML front-matter `status:` is set to `P0-D06` (not `STUB`)
- [ ] Original stub warning has been removed from the document
- [ ] File `docs/diagrams/ingress-bridge-flow.mermaid` exists and contains `flowchart` or `sequenceDiagram`
- [ ] The string `core_ingest_ts` appears in the ADR (timestamp stamping responsibility)
- [ ] The string `50 GB` or `50GB` appears (spool size)
- [ ] The string `500 msg/s` or `500msg/s` appears (rate limit)
- [ ] A failure modes table exists (contains `Failure` and `Recovery` headers)

### Human Review

- [ ] All 8 responsibilities from the task spec are addressed
- [ ] Delivery guarantee is explicitly stated as at-least-once
- [ ] Failure modes table covers NATS, Kafka, MinIO, spool full, schema errors, clock drift
- [ ] Prometheus metrics are listed with types, labels, and descriptions
- [ ] Deployment section includes minimum instances, vCPU, RAM, NVMe sizing
- [ ] Live vs. replay lane separation is specified with priority rules
- [ ] Idempotent key format matches the Kafka contract (`camera_id` as partition key)
- [ ] `core_ingest_ts` stamping rules are consistent with `docs/time-sync-policy.md`
- [ ] Mermaid diagram renders correctly at mermaid.live (3 flows: normal, outage, replay)
- [ ] A Dev agent (P1-V02) can implement against this spec without asking clarifying questions
