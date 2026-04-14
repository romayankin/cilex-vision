"""Pydantic Settings for the inference worker.

Loaded from YAML with environment variable overrides (prefix ``INFERENCE_``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings


class TritonConfig(BaseModel):
    url: str = "localhost:8001"
    detector_model: str = "yolov8l"
    embedder_model: str = "osnet"
    detector_input_name: str = "images"
    detector_output_name: str = "output0"
    embedder_input_name: str = "images"
    embedder_output_name: str = "embeddings"


class KafkaConfig(BaseModel):
    bootstrap_servers: str = "localhost:9092"
    consumer_group: str = "detector-worker"
    input_topic: str = "frames.decoded.refs"
    tracklet_topic: str = "tracklets.local"
    embedding_topic: str = "mtmc.active_embeddings"
    detection_topic: str = "bulk.detections"
    security_protocol: str = "PLAINTEXT"
    sasl_mechanism: Optional[str] = None
    sasl_username: Optional[str] = None
    sasl_password: Optional[str] = None
    ssl_ca_file: Optional[str] = None
    ssl_cert_file: Optional[str] = None
    ssl_key_file: Optional[str] = None
    auto_offset_reset: str = "latest"
    poll_timeout_ms: int = 1000
    max_poll_records: int = 10


class MinioConfig(BaseModel):
    endpoint: str = "localhost:9000"
    access_key: str = "minioadmin"
    secret_key: str = "minioadmin"
    secure: bool = False
    debug_bucket: str = "debug-traces"


class TrackerConfig(BaseModel):
    track_thresh: float = 0.5
    match_thresh: float = 0.8
    second_match_thresh: float = 0.5
    max_lost_frames: int = 50
    min_hits: int = 3


class DetectorConfig(BaseModel):
    confidence_threshold: float = 0.40
    nms_iou_threshold: float = 0.45
    input_size: int = 640
    num_classes: int = 7


class DebugConfig(BaseModel):
    sample_rate_pct: float = 2.0
    low_confidence_threshold: float = 0.45
    enabled: bool = True


class ThumbnailConfig(BaseModel):
    enabled: bool = True
    bucket: str = "thumbnails"
    max_per_track: int = 5
    min_confidence: float = 0.50
    max_width: int = 224
    quality: int = 80


class Settings(BaseSettings):
    model_config = {"env_prefix": "INFERENCE_", "env_nested_delimiter": "__"}

    triton: TritonConfig = Field(default_factory=TritonConfig)
    kafka: KafkaConfig = Field(default_factory=KafkaConfig)
    minio: MinioConfig = Field(default_factory=MinioConfig)
    tracker: TrackerConfig = Field(default_factory=TrackerConfig)
    detector: DetectorConfig = Field(default_factory=DetectorConfig)
    debug: DebugConfig = Field(default_factory=DebugConfig)
    thumbnail: ThumbnailConfig = Field(default_factory=ThumbnailConfig)
    metrics_port: int = 9090
    log_level: str = "INFO"

    @classmethod
    def from_yaml(cls, path: Path | str) -> Settings:
        """Load settings from a YAML file with env-var overrides."""
        p = Path(path)
        if p.exists():
            with open(p) as fh:
                data = yaml.safe_load(fh) or {}
            return cls(**data)
        return cls()
