"""Jetson-specific configuration extending the base edge agent config."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class CameraConfig(BaseModel):
    """Single camera definition (mirrors base edge agent)."""

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


class MotionConfig(BaseModel):
    """Motion detector tuning parameters."""

    pixel_threshold: int = 25
    motion_threshold: float = 0.02
    scene_change_threshold: float = 0.80
    reference_update_interval_s: int = 300


class BufferConfig(BaseModel):
    """Local ring-buffer for NATS outages."""

    max_bytes: int = Field(default=2 * 1024 * 1024 * 1024)  # 2 GB (Jetson has less disk)
    path: str = "/var/lib/edge-agent/buffer"
    replay_rate_limit: int = 100


class DetectorConfig(BaseModel):
    """TensorRT detector settings."""

    engine_path: str = "/models/yolov8n-int8.engine"
    model_input_size: tuple[int, int] = (640, 640)
    confidence_threshold: float = 0.40
    nms_iou_threshold: float = 0.45
    max_detections: int = 100
    thermal_throttle_warn_ms: float = 100.0


class JetsonHardwareConfig(BaseModel):
    """Jetson hardware settings."""

    power_mode: str = "MAXN"
    dla_core: int = -1  # -1 = GPU only


class JetsonSettings(BaseSettings):
    """Root configuration for Jetson edge agent, loaded from YAML with env overrides."""

    model_config = SettingsConfigDict(env_prefix="JETSON_")

    site_id: str = "site-a"
    cameras: list[CameraConfig] = []
    nats: NatsConfig = NatsConfig()
    motion: MotionConfig = MotionConfig()
    buffer: BufferConfig = BufferConfig()
    detector: DetectorConfig = DetectorConfig()
    hardware: JetsonHardwareConfig = JetsonHardwareConfig()
    metrics_port: int = 9090
    log_level: str = "INFO"
    model_name: str = "yolov8n"
    model_version: str = "1.0.0"

    @classmethod
    def from_yaml(cls, path: str | Path = "config.yaml") -> JetsonSettings:
        """Load settings from a YAML file, with env-var overrides."""
        with open(path) as f:
            data: dict[str, Any] = yaml.safe_load(f) or {}
        return cls(**data)
