"""Prometheus metrics for the MTMC Re-ID association service."""

from prometheus_client import Counter, Gauge, Histogram

MATCHES_TOTAL = Counter(
    "mtmc_matches_total",
    "Total successful cross-camera Re-ID matches",
    ["site_id"],
)

REJECTS_TOTAL = Counter(
    "mtmc_rejects_total",
    "Total rejected match candidates",
    ["site_id", "reason"],
)

MATCH_SCORE = Histogram(
    "mtmc_match_score",
    "Distribution of combined match scores for accepted matches",
    buckets=[0.5, 0.55, 0.6, 0.65, 0.7, 0.75, 0.8, 0.85, 0.9, 0.95, 1.0],
)

FAISS_INDEX_SIZE = Gauge(
    "mtmc_faiss_index_size",
    "Current number of embeddings in the FAISS index",
)

CHECKPOINT_SIZE_BYTES = Gauge(
    "mtmc_checkpoint_size_bytes",
    "Size of the latest checkpoint in bytes",
)

CHECKPOINT_LAG_SECONDS = Gauge(
    "mtmc_checkpoint_lag_seconds",
    "Seconds since the last successful checkpoint",
)

REBALANCE_DURATION = Histogram(
    "mtmc_rebalance_duration_seconds",
    "Duration of Kafka consumer rebalance events",
    buckets=[0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0],
)

EMBEDDINGS_CONSUMED = Counter(
    "mtmc_embeddings_consumed_total",
    "Total embeddings consumed from Kafka",
)
