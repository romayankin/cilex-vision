#!/usr/bin/env python3
"""Dataclasses shared by the stress-test harness."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from common import REPO_ROOT, isoformat_utc


DEFAULT_REPORT_PATH = REPO_ROOT / "docs" / "evaluation-results" / "stress-test-report.md"
DEFAULT_PARAMS_PATH = REPO_ROOT / "scripts" / "cost-model" / "params.yaml"


@dataclass(slots=True)
class TestConfig:
    """Runtime configuration for the stress-test harness."""

    duration_s: int
    camera_count: int
    prometheus_url: str
    query_api_url: str
    chaos_enabled: bool
    camera_fps: int = 5
    query_qps: int = 10
    kafka_bootstrap: str = "localhost:19092"
    kafka_frame_topic: str = "frames.sampled.refs"
    kafka_security_protocol: str = "PLAINTEXT"
    minio_url: str = "localhost:9000"
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "minioadmin123"
    minio_secure: bool = False
    source_bucket: str = "frame-blobs"
    source_width_px: int = 1280
    source_height_px: int = 720
    camera_prefix: str = "stress-cam"
    site_id: str = "pilot-site"
    metrics_interval_s: float = 15.0
    replay_frame_dir: Path | None = None
    report_path: Path = DEFAULT_REPORT_PATH
    cost_model_params_path: Path = DEFAULT_PARAMS_PATH
    query_jwt_secret: str = "pilot-jwt-secret-change-me"
    query_cookie_name: str = "access_token"
    query_role: str = "admin"
    chaos_kafka_container_template: str = "pilot-kafka"
    chaos_service_container_map: dict[str, str] = field(
        default_factory=lambda: {
            "decode-worker": "pilot-decode-service",
            "detector-worker": "pilot-inference-worker",
            "bulk-collector": "pilot-bulk-collector",
            "event-engine": "event-engine",
            "clip-service": "clip-service",
            "mtmc-service": "mtmc-service",
        }
    )
    chaos_network_name: str = "cilex-pilot"
    chaos_wan_target_container: str = "pilot-edge-agent"

    @property
    def camera_ids(self) -> list[str]:
        return [f"{self.camera_prefix}-{index:02d}" for index in range(1, self.camera_count + 1)]


@dataclass(slots=True)
class MetricSnapshot:
    """Single Prometheus sampling point."""

    collected_at: datetime
    latency_quantiles: dict[str, dict[str, float | None]]
    throughput_rates: dict[str, float | None]
    resource_cpu_cores: dict[str, float | None]
    resource_memory_bytes: dict[str, float | None]
    kafka_consumer_lag: dict[str, float | None]
    error_rates: dict[str, float | None]
    service_health: dict[str, float | None]
    raw_scalars: dict[str, float | None] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "collected_at": isoformat_utc(self.collected_at),
            "latency_quantiles": self.latency_quantiles,
            "throughput_rates": self.throughput_rates,
            "resource_cpu_cores": self.resource_cpu_cores,
            "resource_memory_bytes": self.resource_memory_bytes,
            "kafka_consumer_lag": self.kafka_consumer_lag,
            "error_rates": self.error_rates,
            "service_health": self.service_health,
            "raw_scalars": self.raw_scalars,
        }


@dataclass(slots=True)
class ChaosResult:
    """Result of a single reversible chaos scenario."""

    name: str
    target: str
    start_time: datetime
    end_time: datetime
    recovery_time_s: float | None
    data_loss: bool | None
    success: bool
    notes: str
    pre_row_count: int | None = None
    post_row_count: int | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "target": self.target,
            "start_time": isoformat_utc(self.start_time),
            "end_time": isoformat_utc(self.end_time),
            "recovery_time_s": self.recovery_time_s,
            "data_loss": self.data_loss,
            "success": self.success,
            "notes": self.notes,
            "pre_row_count": self.pre_row_count,
            "post_row_count": self.post_row_count,
        }
