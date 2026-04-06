"""Prometheus metrics for the inference worker."""

from prometheus_client import Counter, Gauge, Histogram

DETECTIONS_TOTAL = Counter(
    "inference_detections_total",
    "Total detections produced",
    ["object_class"],
)

TRACKS_ACTIVE = Gauge(
    "inference_tracks_active",
    "Currently active tracks",
    ["camera_id"],
)

TRACKS_CLOSED = Counter(
    "inference_tracks_closed_total",
    "Total tracks that reached TERMINATED state",
    ["camera_id"],
)

INFERENCE_LATENCY = Histogram(
    "inference_latency_ms",
    "Detection inference latency in milliseconds",
    buckets=[5, 10, 25, 50, 100, 250, 500, 1000, 2000],
)

EMBEDDING_LATENCY = Histogram(
    "inference_embedding_latency_ms",
    "Re-ID embedding inference latency in milliseconds",
    buckets=[5, 10, 25, 50, 100, 250, 500, 1000],
)

FRAMES_CONSUMED = Counter(
    "inference_frames_consumed_total",
    "Total frames consumed from Kafka",
)

PUBLISH_ERRORS = Counter(
    "inference_publish_errors_total",
    "Total Kafka publish failures",
    ["topic"],
)

CONSUMER_LAG = Gauge(
    "inference_consumer_lag",
    "Kafka consumer lag",
    ["topic", "partition"],
)
