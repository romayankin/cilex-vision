"""Ingress Bridge configuration loaded from YAML with env-var overrides."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class NatsTlsConfig(BaseModel):
    """mTLS settings for the bridge -> NATS connection."""

    cert_file: str
    key_file: str
    ca_file: str


class NatsConfig(BaseModel):
    """NATS JetStream connectivity and consumer settings."""

    url: str = "nats://localhost:4222"
    tls: NatsTlsConfig | None = None
    durable_prefix: str = "ingress-bridge"
    live_subject_template: str = "frames.live.{site_id}.>"
    replay_subject_template: str = "frames.replay.{site_id}.>"
    dlq_subject_template: str = "frames.dlq.{site_id}"
    stream_name_template: str | None = None
    ack_wait_s: int = 30
    max_redeliver: int = 3
    fetch_batch_size: int = 10
    fetch_timeout_s: float = 1.0


class KafkaConfig(BaseModel):
    """Kafka producer settings."""

    bootstrap_servers: str = "localhost:9092"
    security_protocol: str = "SASL_SSL"
    sasl_mechanism: str = "SCRAM-SHA-256"
    sasl_username: str = "svc-ingress-bridge"
    sasl_password: str = "change-me"
    ssl_ca_file: str | None = None
    ssl_cert_file: str | None = None
    ssl_key_file: str | None = None
    client_id: str = "ingress-bridge"
    acks: str = "all"
    compression_type: str = "zstd"
    linger_ms: int = 5
    batch_size: int = 65_536
    enable_idempotence: bool = True
    request_timeout_ms: int = 30_000
    topic_frames_sampled_refs: str = "frames.sampled.refs"


class SchemaRegistryConfig(BaseModel):
    """Schema Registry endpoint and cache settings."""

    url: str = "http://localhost:8081"
    frame_ref_subject: str = "frames.sampled.refs-value"
    cache_ttl_s: int = 300
    enabled: bool = True


class MinioConfig(BaseModel):
    """MinIO object-store settings for auxiliary blob offload."""

    endpoint: str = "localhost:9000"
    access_key: str = "minioadmin"
    secret_key: str = "minioadmin"
    secure: bool = False
    bucket_blobs: str = "frame-blobs"


class SpoolConfig(BaseModel):
    """Disk spool settings for Kafka and blob-store failures."""

    path: str = "/var/lib/ingress-bridge/spool"
    max_bytes: int = 50 * 1024 * 1024 * 1024
    resume_pct: int = 80
    replay_rate_limit_msg_per_sec: int = 1000
    spool_drain_rate_limit_msg_per_sec: int = 1000
    replay_limit_pct: int = 50
    spool_drain_pct: int = 80


class SiteConfig(BaseModel):
    """Per-site rate-limit overrides."""

    site_id: str
    rate_limit_msg_per_sec: int = 500
    replay_rate_limit_msg_per_sec: int | None = None
    spool_drain_rate_limit_msg_per_sec: int | None = None


class Settings(BaseSettings):
    """Root bridge settings."""

    model_config = SettingsConfigDict(
        env_prefix="BRIDGE_",
        env_nested_delimiter="__",
    )

    nats: NatsConfig = NatsConfig()
    kafka: KafkaConfig = KafkaConfig()
    schema_registry: SchemaRegistryConfig = SchemaRegistryConfig()
    minio: MinioConfig = MinioConfig()
    spool: SpoolConfig = SpoolConfig()
    sites: list[SiteConfig] = Field(default_factory=list)
    metrics_port: int = 9091
    health_port: int = 8080
    log_level: str = "INFO"

    @classmethod
    def from_yaml(cls, path: str | Path = "config.yaml") -> Settings:
        """Load settings from YAML, then apply env-var overrides."""
        with open(path, encoding="utf-8") as handle:
            data: dict[str, Any] = yaml.safe_load(handle) or {}
        return cls(**data)

    def site_index(self) -> dict[str, SiteConfig]:
        """Return a site-id keyed lookup for rate-limit decisions."""
        return {site.site_id: site for site in self.sites}
