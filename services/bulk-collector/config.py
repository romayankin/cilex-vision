"""Bulk Collector configuration loaded from YAML with env-var overrides."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class KafkaTopicBinding(BaseModel):
    """One Kafka topic plus the consumer group that owns it."""

    topic: str
    group_id: str
    expected_schema: str | None = None
    enabled: bool = True


class KafkaConfig(BaseModel):
    """Kafka consumer settings."""

    bootstrap_servers: str = "localhost:9092"
    security_protocol: str = "SASL_SSL"
    sasl_mechanism: str = "SCRAM-SHA-256"
    sasl_username: str = "svc-bulk-collector"
    sasl_password: str = "change-me"
    ssl_ca_file: str | None = None
    ssl_cert_file: str | None = None
    ssl_key_file: str | None = None
    client_id: str = "bulk-collector"
    auto_offset_reset: str = "earliest"
    poll_timeout_ms: int = 500
    max_poll_records: int = 1000
    topic_bindings: list[KafkaTopicBinding] = Field(
        default_factory=lambda: [
            KafkaTopicBinding(
                topic="bulk.detections",
                group_id="bulk-collector-detections",
                expected_schema="vidanalytics.v1.detection.Detection",
            ),
        ]
    )


class SchemaRegistryConfig(BaseModel):
    """Optional Schema Registry settings for protobuf deserialization."""

    url: str | None = None


class DatabaseConfig(BaseModel):
    """TimescaleDB / PostgreSQL connectivity."""

    dsn: str = "postgresql://postgres:postgres@localhost:5432/cilex"
    min_pool_size: int = 1
    max_pool_size: int = 5
    command_timeout_s: float = 30.0


class CollectorConfig(BaseModel):
    """In-memory batching and dedup settings."""

    batch_size: int = 1000
    max_age_ms: int = 500
    flush_interval_ms: int = 100
    dedup_ttl_s: int = 600
    dedup_max_keys: int = 250_000


class Settings(BaseSettings):
    """Root bulk collector settings."""

    model_config = SettingsConfigDict(
        env_prefix="BULK_",
        env_nested_delimiter="__",
    )

    kafka: KafkaConfig = KafkaConfig()
    schema_registry: SchemaRegistryConfig = SchemaRegistryConfig()
    database: DatabaseConfig = DatabaseConfig()
    collector: CollectorConfig = CollectorConfig()
    metrics_port: int = 9091
    health_port: int = 8080
    log_level: str = "INFO"

    @classmethod
    def from_yaml(cls, path: str | Path = "config.yaml") -> Settings:
        """Load settings from YAML and then apply env overrides."""
        with open(path, encoding="utf-8") as handle:
            data: dict[str, Any] = yaml.safe_load(handle) or {}
        return cls(**data)

