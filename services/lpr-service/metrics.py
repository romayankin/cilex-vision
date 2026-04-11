"""Prometheus metrics for the LPR service."""

from prometheus_client import Counter, Histogram

PLATES_DETECTED_TOTAL = Counter(
    "plates_detected_total",
    "Total license plates detected by object class.",
    ["object_class"],
)

PLATES_RECOGNIZED_TOTAL = Counter(
    "plates_recognized_total",
    "Total license plates recognized by inferred country format.",
    ["country_format"],
)

QUALITY_REJECTED_TOTAL = Counter(
    "quality_rejected_total",
    "Total LPR plate crops rejected by the quality gate.",
    ["reason"],
)

DETECTION_LATENCY_MS = Histogram(
    "detection_latency_ms",
    "Plate detector inference latency in milliseconds.",
    buckets=[5, 10, 25, 50, 100, 250, 500, 1000],
)

OCR_LATENCY_MS = Histogram(
    "ocr_latency_ms",
    "OCR inference latency in milliseconds.",
    buckets=[5, 10, 25, 50, 100, 250, 500, 1000],
)

PIPELINE_ERRORS_TOTAL = Counter(
    "pipeline_errors_total",
    "Total unrecoverable LPR pipeline errors.",
)
