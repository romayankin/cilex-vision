"""Prometheus metrics for the Metadata Bulk Collector."""

from prometheus_client import Counter, Gauge, Histogram

ROWS_WRITTEN = Counter(
    "bulk_rows_written_total",
    "Rows successfully written to PostgreSQL / TimescaleDB.",
    ["table"],
)

BATCH_SIZE = Histogram(
    "bulk_batch_size_histogram",
    "Number of rows flushed per table write.",
    ["table"],
    buckets=[1, 10, 50, 100, 250, 500, 1000, 2000, 5000],
)

WRITE_LATENCY = Histogram(
    "bulk_write_latency_ms",
    "COPY write latency in milliseconds.",
    ["table"],
    buckets=[1, 5, 10, 25, 50, 100, 250, 500, 1000, 5000],
)

WRITE_ERRORS = Counter(
    "bulk_write_errors_total",
    "COPY write failures.",
    ["table"],
)

CONSUMER_LAG = Gauge(
    "bulk_consumer_lag",
    "Estimated Kafka consumer lag by group, topic, and partition.",
    ["group", "topic", "partition"],
)

MESSAGES_CONSUMED = Counter(
    "bulk_messages_consumed_total",
    "Kafka messages consumed by schema.",
    ["topic", "schema"],
)

MESSAGES_REJECTED = Counter(
    "bulk_messages_rejected_total",
    "Kafka messages rejected before staging.",
    ["topic", "reason"],
)

ROWS_STAGED = Gauge(
    "bulk_rows_staged",
    "Rows currently waiting in the in-memory batch collector.",
    ["table"],
)

DUPLICATES_SKIPPED = Counter(
    "bulk_duplicates_skipped_total",
    "Rows dropped by the bounded in-memory dedup cache.",
    ["table"],
)

