"""Pydantic settings for the LPR service."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic_settings import BaseSettings


class LprSettings(BaseSettings):
    """Runtime configuration for the license plate recognition pipeline."""

    model_config = {"env_prefix": "LPR_", "env_nested_delimiter": "__"}

    enabled: bool = False

    kafka_bootstrap: str = "localhost:9092"
    kafka_group_id: str = "lpr-service"
    kafka_topic: str = "tracklets.local"
    kafka_security_protocol: str = "PLAINTEXT"
    kafka_poll_timeout_s: float = 1.0

    triton_url: str = "localhost:8001"
    plate_detector_model: str = "plate_detector"
    plate_detector_input_name: str = "images"
    plate_detector_output_name: str = "plate_detections"
    plate_detector_input_size: int = 640
    plate_detection_confidence_threshold: float = 0.35
    plate_nms_iou_threshold: float = 0.40

    ocr_model: str = "plate_ocr"
    ocr_input_name: str = "images"
    ocr_output_name: str = "logits"
    ocr_input_width: int = 160
    ocr_input_height: int = 48
    ocr_confidence_threshold: float = 0.60
    ocr_alphabet: str = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"

    minio_url: str = "localhost:9000"
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "minioadmin123"
    minio_secure: bool = False
    minio_frame_bucket: str = "decoded-frames"
    frame_key_templates: tuple[str, ...] = (
        "{camera_id}/{date}/{frame_seq}.jpg",
        "{camera_id}/{frame_seq}.jpg",
    )
    frame_lookup_tolerance_s: float = 120.0

    db_dsn: str = "postgresql://localhost:5432/cilex"

    min_plate_height: int = 20
    min_plate_width: int = 60
    sharpness_threshold: float = 40.0
    min_aspect_ratio: float = 2.0
    max_aspect_ratio: float = 5.0

    flush_batch_size: int = 50
    flush_interval_s: float = 1.0
    max_samples_per_track: int = 5

    metrics_port: int = 8080
    log_level: str = "INFO"
    model_version: str = "plate-detector+ocr-1.0.0"

    @classmethod
    def from_yaml(cls, path: Path | str) -> LprSettings:
        """Load settings from YAML with env-var overrides."""
        config_path = Path(path)
        if config_path.exists():
            with config_path.open() as handle:
                data = yaml.safe_load(handle) or {}
            return cls(**data)
        return cls()
