"""Pydantic Settings for the MTMC Re-ID association service.

Loaded from YAML with environment variable overrides (prefix ``MTMC_``).
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic_settings import BaseSettings


class MTMCSettings(BaseSettings):
    model_config = {"env_prefix": "MTMC_", "env_nested_delimiter": "__"}

    kafka_bootstrap: str = "localhost:9092"
    kafka_group_id: str = "mtmc-service"
    kafka_topic: str = "mtmc.active_embeddings"
    kafka_security_protocol: str = "PLAINTEXT"
    kafka_poll_timeout_s: float = 1.0
    kafka_max_poll_records: int = 50

    minio_url: str = "localhost:9000"
    minio_access_key: str = "minioadmin"
    minio_secret_key: str = "minioadmin"
    minio_secure: bool = False
    minio_bucket: str = "mtmc-checkpoints"

    db_dsn: str = "postgresql://localhost:5432/cilex"

    site_id: str = "default"
    topology_refresh_s: int = 300
    active_horizon_minutes: int = 30
    faiss_k: int = 20
    match_threshold: float = 0.65
    checkpoint_local_path: str = "/data/mtmc-checkpoint"
    checkpoint_local_interval_s: int = 60
    checkpoint_minio_interval_s: int = 300
    score_weight_cosine: float = 0.6
    score_weight_transit: float = 0.3
    score_weight_attribute: float = 0.1

    # Zone sharding (optional)
    zone_id: str | None = None  # None = no sharding
    cross_zone_topic: str = "mtmc.cross_zone"
    cross_zone_match_threshold: float = 0.55
    cross_zone_batch_interval_s: float = 5.0

    metrics_port: int = 8080
    log_level: str = "INFO"

    @classmethod
    def from_yaml(cls, path: Path | str) -> MTMCSettings:
        """Load settings from a YAML file with env-var overrides."""
        p = Path(path)
        if p.exists():
            with open(p) as fh:
                data = yaml.safe_load(fh) or {}
            return cls(**data)
        return cls()
