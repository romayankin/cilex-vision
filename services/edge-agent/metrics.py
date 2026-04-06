"""Prometheus metrics for the Edge Agent.

All metrics use the ``edge_`` prefix per service naming conventions.
"""

from prometheus_client import Counter, Gauge, Histogram

CAMERA_UPTIME = Gauge(
    "edge_camera_uptime_ratio",
    "Camera connection uptime ratio (0.0–1.0)",
    ["camera_id"],
)

DECODE_ERRORS = Counter(
    "edge_decode_errors_total",
    "Total GStreamer decoder errors",
    ["camera_id"],
)

MOTION_FRAMES = Counter(
    "edge_motion_frames_total",
    "Frames that passed the motion filter",
    ["camera_id"],
)

STATIC_FILTERED = Counter(
    "edge_static_frames_filtered_total",
    "Frames filtered by motion detector (no motion)",
    ["camera_id"],
)

NATS_LATENCY = Histogram(
    "edge_nats_publish_latency_ms",
    "NATS JetStream publish latency in milliseconds",
    ["camera_id"],
    buckets=[1, 5, 10, 25, 50, 100, 250, 500, 1000],
)

BUFFER_FILL = Gauge(
    "edge_buffer_fill_bytes",
    "Current local ring-buffer usage in bytes",
)
