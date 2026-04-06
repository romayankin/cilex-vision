"""Edge Agent configuration — Pydantic Settings loaded from YAML."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class CameraConfig(BaseModel):
    """Single camera definition."""

    camera_id: str
    rtsp_url: str
    enabled: bool = True


class NatsTlsConfig(BaseModel):
    """mTLS certificate paths for NATS connection."""

    cert_file: str
    key_file: str
    ca_file: str


class NatsConfig(BaseModel):
    """NATS JetStream connection settings."""

    url: str = "nats://localhost:4222"
    tls: NatsTlsConfig | None = None


class MinioConfig(BaseModel):
    """MinIO object-store settings for frame upload."""

    endpoint: str = "localhost:9000"
    access_key: str = "minioadmin"
    secret_key: str = "minioadmin"
    bucket: str = "frame-blobs"
    secure: bool = False


class MotionConfig(BaseModel):
    """Motion detector tuning parameters."""

    pixel_threshold: int = 25
    motion_threshold: float = 0.02
    scene_change_threshold: float = 0.80
    reference_update_interval_s: int = 300


class BufferConfig(BaseModel):
    """Local ring-buffer for NATS outages."""

    max_bytes: int = Field(default=10 * 1024 * 1024 * 1024)  # 10 GB
    path: str = "/var/lib/edge-agent/buffer"
    replay_rate_limit: int = 100  # messages per second


class Settings(BaseSettings):
    """Root configuration loaded from YAML with env-var overrides."""

    model_config = SettingsConfigDict(env_prefix="EDGE_")

    site_id: str = "site-a"
    cameras: list[CameraConfig] = []
    nats: NatsConfig = NatsConfig()
    minio: MinioConfig = MinioConfig()
    motion: MotionConfig = MotionConfig()
    buffer: BufferConfig = BufferConfig()
    metrics_port: int = 9090
    log_level: str = "INFO"

    @classmethod
    def from_yaml(cls, path: str | Path = "config.yaml") -> Settings:
        """Load settings from a YAML file, with env-var overrides."""
        with open(path) as f:
            data: dict[str, Any] = yaml.safe_load(f) or {}
        return cls(**data)
