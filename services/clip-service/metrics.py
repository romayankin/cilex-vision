"""Prometheus metrics for the clip pipeline service."""

from prometheus_client import Counter, Histogram

CLIP_EXTRACTED_TOTAL = Counter(
    "clip_extracted_total",
    "Total event clips successfully extracted",
)

CLIP_EXTRACTION_LATENCY_MS = Histogram(
    "clip_extraction_latency_ms",
    "Latency of event clip extraction in milliseconds",
    buckets=[10, 50, 100, 250, 500, 1000, 2500, 5000, 10000, 30000],
)

CLIP_SIZE_BYTES = Histogram(
    "clip_size_bytes",
    "Distribution of extracted clip sizes in bytes",
    buckets=[10_000, 50_000, 100_000, 500_000, 1_000_000, 5_000_000, 10_000_000],
)

CLIP_EXTRACTION_ERRORS_TOTAL = Counter(
    "clip_extraction_errors_total",
    "Total clip extraction failures",
    ["reason"],
)

CLIP_THUMBNAILS_GENERATED_TOTAL = Counter(
    "clip_thumbnails_generated_total",
    "Total thumbnails generated for extracted clips",
)

CLIP_EVENTS_CONSUMED_TOTAL = Counter(
    "clip_events_consumed_total",
    "Total event messages consumed from Kafka",
)

CLIP_EVENTS_SKIPPED_TOTAL = Counter(
    "clip_events_skipped_total",
    "Total event messages skipped by the clip pipeline",
    ["reason"],
)
