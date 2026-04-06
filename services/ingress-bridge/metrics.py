"""Prometheus metrics for the Ingress Bridge."""

from prometheus_client import Counter, Gauge, Histogram

MESSAGES_RECEIVED = Counter(
    "bridge_messages_received_total",
    "Messages received from NATS JetStream.",
    ["site_id", "lane"],
)

MESSAGES_PRODUCED = Counter(
    "bridge_messages_produced_total",
    "Messages successfully produced to Kafka.",
    ["site_id", "topic"],
)

MESSAGES_SPOOLED = Counter(
    "bridge_messages_spooled_total",
    "Messages written to the local spool.",
    ["site_id", "reason"],
)

SPOOL_DRAINED = Counter(
    "bridge_spool_drain_msg_total",
    "Spooled messages successfully drained to Kafka.",
    ["site_id"],
)

SCHEMA_REJECTION = Counter(
    "bridge_schema_rejection_total",
    "Messages rejected by schema validation.",
    ["site_id", "reason"],
)

RATE_LIMITED = Counter(
    "bridge_rate_limited_total",
    "Messages delayed by a per-site rate limiter.",
    ["site_id", "lane"],
)

BLOB_OFFLOAD = Counter(
    "bridge_blob_offload_total",
    "Auxiliary blobs uploaded to MinIO.",
    ["site_id", "bucket"],
)

DLQ_PUBLISHED = Counter(
    "bridge_dlq_published_total",
    "Messages published to the NATS DLQ.",
    ["site_id"],
)

SPOOL_FULL = Counter(
    "bridge_spool_full_total",
    "Spool high-watermark events.",
)

SPOOL_CORRUPT = Counter(
    "bridge_spool_corrupt_total",
    "Corrupt spool files quarantined during drain.",
)

CLOCK_DRIFT = Counter(
    "bridge_clock_drift_detected_total",
    "Messages where core_ingest_ts was earlier than edge_receive_ts.",
    ["site_id"],
)

NATS_CONSUMER_LAG = Gauge(
    "bridge_nats_consumer_lag",
    "Approximate pending / unacked NATS messages per site and lane.",
    ["site_id", "lane"],
)

SPOOL_DEPTH_BYTES = Gauge(
    "bridge_spool_depth_bytes",
    "Current spool usage in bytes.",
)

SPOOL_DEPTH_MESSAGES = Gauge(
    "bridge_spool_depth_messages",
    "Current spool file count.",
)

SPOOL_FILL_PCT = Gauge(
    "bridge_spool_fill_pct",
    "Spool usage as a percentage of configured capacity.",
)

KAFKA_INFLIGHT = Gauge(
    "bridge_kafka_producer_inflight",
    "Current in-flight Kafka produce requests.",
)

RATE_LIMIT_HEADROOM = Gauge(
    "bridge_rate_limit_headroom_pct",
    "Estimated remaining headroom for the live per-site rate limit.",
    ["site_id"],
)

NATS_TO_KAFKA_LATENCY = Histogram(
    "bridge_nats_to_kafka_latency_ms",
    "Time from NATS receipt to Kafka ack in milliseconds.",
    ["site_id", "lane"],
    buckets=[1, 5, 10, 25, 50, 100, 250, 500, 1000],
)

KAFKA_PRODUCE_LATENCY = Histogram(
    "bridge_kafka_produce_latency_ms",
    "Kafka produce latency in milliseconds.",
    ["topic"],
    buckets=[1, 5, 10, 25, 50, 100, 250, 500, 1000],
)

BLOB_OFFLOAD_LATENCY = Histogram(
    "bridge_blob_offload_latency_ms",
    "MinIO auxiliary blob offload latency in milliseconds.",
    ["bucket"],
    buckets=[10, 50, 100, 250, 500, 1000, 5000],
)
