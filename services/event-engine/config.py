"""Pydantic settings for the event engine service."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic_settings import BaseSettings


class EventEngineSettings(BaseSettings):
    """Service settings loaded from YAML with env-var overrides."""

    model_config = {"env_prefix": "EVENT_", "env_nested_delimiter": "__"}

    kafka_bootstrap: str = "localhost:9092"
    kafka_group_id: str = "event-engine"
    kafka_input_topic: str = "tracklets.local"
    kafka_output_topic: str = "events.raw"
    kafka_security_protocol: str = "PLAINTEXT"
    kafka_poll_timeout_s: float = 1.0

    db_dsn: str = "postgresql://localhost:5432/cilex"

    stopped_threshold: float = 0.005
    stopped_duration_s: float = 3.0
    stopped_resume_threshold: float = 0.01
    stopped_resume_duration_s: float = 1.0
    loitering_duration_s: float = 30.0

    tick_interval_s: float = 1.0
    motion_stillness_ms: int = 500
    motion_end_duration_s: float = 2.0
    motion_events_enabled: bool = False

    metrics_port: int = 8080
    log_level: str = "INFO"

    @classmethod
    def from_yaml(cls, path: Path | str) -> EventEngineSettings:
        """Load settings from YAML when the file exists."""
        config_path = Path(path)
        if config_path.exists():
            with open(config_path) as fh:
                data = yaml.safe_load(fh) or {}
            return cls(**data)
        return cls()
