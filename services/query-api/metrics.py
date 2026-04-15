"""Prometheus metrics for the Query API."""

from prometheus_client import Counter, Gauge, Histogram

CONCURRENT_REQUESTS = Gauge(
    "query_concurrent_requests",
    "Number of API requests currently being processed",
)

CONCURRENT_REQUESTS_HIGH_WATER = Gauge(
    "query_concurrent_requests_high_water",
    "Peak concurrent requests since last reset (resets every 5 minutes)",
)

QUERY_REQUESTS = Counter(
    "query_requests_total",
    "Total API requests by endpoint and method",
    ["method", "endpoint", "status"],
)

QUERY_LATENCY = Histogram(
    "query_latency_ms",
    "Request latency in milliseconds",
    ["method", "endpoint"],
    buckets=[10, 25, 50, 100, 200, 500, 1000, 2000],
)

QUERY_DB_LATENCY = Histogram(
    "query_db_latency_ms",
    "Database query latency in milliseconds",
    ["query_type"],
    buckets=[5, 10, 25, 50, 100, 250, 500, 1000],
)

AUTH_FAILURES = Counter(
    "query_auth_failures_total",
    "Authentication and authorization failures",
    ["reason"],
)

AUDIT_WRITES = Counter(
    "query_audit_writes_total",
    "Audit log entries written",
)

AUDIT_ERRORS = Counter(
    "query_audit_errors_total",
    "Audit log write failures",
)
