"""Pydantic Settings for the decode service.

Loaded from YAML with environment variable overrides (prefix ``DECODE_``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class KafkaConfig(BaseModel):
    bootstrap_servers: str = "localhost:9092"
    consumer_group: str = "decode-worker"
    input_topic: str = "frames.sampled.refs"
    output_topic: str = "frames.decoded.refs"
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
    source_bucket: str = "frame-blobs"
    decoded_bucket: str = "decoded-frames"


class DecodeConfig(BaseModel):
    """Decoder tuning parameters."""

    output_width: int = 1280
    output_height: int = 720
    jpeg_quality: int = 90
    default_color_space: str = "bt601"


class SamplerConfig(BaseModel):
    """Frame sampling parameters."""

    target_fps: float = 5.0


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DECODE_", env_nested_delimiter="__")

    kafka: KafkaConfig = Field(default_factory=KafkaConfig)
    minio: MinioConfig = Field(default_factory=MinioConfig)
    decode: DecodeConfig = Field(default_factory=DecodeConfig)
    sampler: SamplerConfig = Field(default_factory=SamplerConfig)
    num_workers: int = 4
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
