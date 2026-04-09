"""Prometheus metrics for the attribute extraction service."""

from prometheus_client import Counter, Histogram

CLASSIFIED_TOTAL = Counter(
    "attr_classified_total",
    "Total attribute classifications produced",
    ["attribute_type", "color_value"],
)

QUALITY_REJECTED_TOTAL = Counter(
    "attr_quality_rejected_total",
    "Total crops rejected by quality gate",
    ["reason"],
)

IR_SKIPPED_TOTAL = Counter(
    "attr_ir_skipped_total",
    "Total crops skipped due to IR/night mode detection",
)

CLASSIFICATION_LATENCY = Histogram(
    "attr_classification_latency_ms",
    "Triton color classification latency in milliseconds",
    buckets=[5, 10, 25, 50, 100, 250, 500, 1000],
)

TRACKS_FLUSHED_TOTAL = Counter(
    "attr_tracks_flushed_total",
    "Total tracks whose aggregated attributes were flushed to DB",
)

DB_WRITE_LATENCY = Histogram(
    "attr_db_write_latency_ms",
    "Database write latency in milliseconds",
    buckets=[1, 5, 10, 25, 50, 100, 250, 500],
)
