"""Prometheus metrics for the decode service."""

from prometheus_client import Counter, Gauge, Histogram

DECODE_ERRORS = Counter(
    "decode_errors_total",
    "Total decode errors by codec",
    ["codec"],
)

FRAMES_DECODED = Counter(
    "decode_frames_decoded_total",
    "Total frames successfully decoded",
)

FRAMES_SAMPLED = Counter(
    "decode_frames_sampled_total",
    "Total frames forwarded after sampling",
)

FRAMES_SKIPPED = Counter(
    "decode_frames_skipped_total",
    "Frames skipped by the sampler",
)

DECODE_LATENCY = Histogram(
    "decode_latency_ms",
    "Frame decode latency in milliseconds",
    buckets=[1, 5, 10, 25, 50, 100, 250, 500, 1000],
)

FRAMES_CONSUMED = Counter(
    "decode_frames_consumed_total",
    "Total frames consumed from Kafka",
)

PUBLISH_ERRORS = Counter(
    "decode_publish_errors_total",
    "Kafka publish failures",
)

CONSUMER_LAG = Gauge(
    "decode_consumer_lag",
    "Kafka consumer lag",
    ["topic", "partition"],
)

COLOR_SPACE_CONVERSIONS = Counter(
    "decode_color_space_conversions_total",
    "Color space conversions performed",
    ["from_space", "to_space"],
)
