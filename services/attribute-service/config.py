"""Pydantic Settings for the attribute extraction service.

Loaded from YAML with environment variable overrides (prefix ``ATTR_``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Tuple

import yaml
from pydantic_settings import BaseSettings


class AttributeSettings(BaseSettings):
    model_config = {"env_prefix": "ATTR_", "env_nested_delimiter": "__"}

    kafka_bootstrap: str = "localhost:9092"
    kafka_group_id: str = "attribute-service"
    kafka_topic: str = "tracklets.local"
    kafka_security_protocol: str = "PLAINTEXT"
    kafka_poll_timeout_s: float = 1.0

    triton_url: str = "localhost:8001"
    triton_model: str = "color_classifier"
    triton_input_name: str = "images"
    triton_output_name: str = "probabilities"

    minio_url: str = "localhost:9000"
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "minioadmin"
    minio_secure: bool = False
    minio_frame_bucket: str = "frame-blobs"

    db_dsn: str = "postgresql://localhost:5432/cilex"

    min_bbox_height: int = 40
    min_sharpness: float = 50.0
    brightness_range: Tuple[int, int] = (30, 220)
    ir_saturation_threshold: int = 15
    max_occlusion_ratio: float = 0.4
    color_confidence_threshold: float = 0.30

    flush_batch_size: int = 50
    flush_interval_s: float = 1.0
    max_observations_per_track: int = 20

    model_name: str = "resnet18-color"
    model_version: str = "1.0.0"

    metrics_port: int = 8080
    health_port: int = 8081
    log_level: str = "INFO"

    @classmethod
    def from_yaml(cls, path: Path | str) -> AttributeSettings:
        """Load settings from a YAML file with env-var overrides."""
        p = Path(path)
        if p.exists():
            with open(p) as fh:
                data = yaml.safe_load(fh) or {}
            return cls(**data)
        return cls()
