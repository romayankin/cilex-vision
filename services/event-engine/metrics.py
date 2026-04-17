"""Prometheus metrics for the event engine service."""

from prometheus_client import Counter, Gauge, Histogram

EVENT_EMITTED_TOTAL = Counter(
    "event_emitted_total",
    "Total events emitted by the event engine",
    ["event_type"],
)

EVENT_ACTIVE_STATE_MACHINES = Gauge(
    "event_active_state_machines",
    "Number of active per-track state machines held in memory",
)

EVENT_STATE_TRANSITIONS_TOTAL = Counter(
    "event_state_transitions_total",
    "Total track state transitions in the event engine",
    ["from_state", "to_state"],
)

EVENT_DB_WRITE_LATENCY_MS = Histogram(
    "event_db_write_latency_ms",
    "Latency of PostgreSQL event writes in milliseconds",
    buckets=[1, 5, 10, 25, 50, 100, 250, 500, 1000],
)

EVENT_TRACKLETS_CONSUMED_TOTAL = Counter(
    "event_tracklets_consumed_total",
    "Total tracklets consumed from Kafka",
)

EVENT_SUPPRESSED_TOTAL = Counter(
    "event_suppressed_total",
    "Events suppressed by deduplication cooldown",
    ["event_type"],
)
