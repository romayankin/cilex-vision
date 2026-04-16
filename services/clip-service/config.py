"""Pydantic settings for the clip pipeline service."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic_settings import BaseSettings


class ClipServiceSettings(BaseSettings):
    """Service settings loaded from YAML with env-var overrides."""

    model_config = {"env_prefix": "CLIP_", "env_nested_delimiter": "__"}

    kafka_bootstrap: str = "localhost:9092"
    kafka_group_id: str = "clip-service"
    kafka_input_topic: str = "events.raw"
    kafka_output_topic: str = "archive.transcode.completed"
    kafka_security_protocol: str = "PLAINTEXT"
    kafka_poll_timeout_s: float = 1.0

    db_dsn: str = "postgresql://localhost:5432/cilex"

    minio_url: str = "localhost:9000"
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "minioadmin123"
    minio_secure: bool = False
    source_bucket: str = "decoded-frames"
    clip_bucket: str = "event-clips"
    thumbnail_bucket: str = "thumbnails"

    pre_roll_s: float = 5.0
    post_roll_s: float = 5.0
    target_bitrate: str = "1500k"
    target_fps: int = 5
    thumbnail_width: int = 320
    thumbnail_height: int = 180
    min_clip_duration_ms: int = 1000

    temp_dir: str = "/tmp/clip-service"
    metrics_port: int = 8080
    health_port: int = 8081
    log_level: str = "INFO"

    @classmethod
    def from_yaml(cls, path: Path | str) -> ClipServiceSettings:
        """Load settings from YAML when present."""
        config_path = Path(path)
        if config_path.exists():
            with open(config_path) as fh:
                data = yaml.safe_load(fh) or {}
            return cls(**data)
        return cls()
