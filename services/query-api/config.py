"""Pydantic Settings for the Query API.

Loaded from YAML with environment variable overrides (prefix ``QUERY_``).
"""

from __future__ import annotations

from pathlib import Path
import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class DatabaseConfig(BaseModel):
    """asyncpg connection settings."""

    dsn: str = "postgresql://cilex:cilex@localhost:5432/cilex"
    min_pool_size: int = 2
    max_pool_size: int = 10
    command_timeout_s: float = 30.0


class MinioConfig(BaseModel):
    endpoint: str = "localhost:9000"
    access_key: str = "minioadmin"
    secret_key: str = "minioadmin"
    secure: bool = False
    signed_url_expiry_s: int = 3600  # 1 hour
    debug_bucket: str = "debug-traces"


class JwtConfig(BaseModel):
    """JWT verification settings."""

    secret_key: str = "change-me-in-production"
    algorithm: str = "HS256"
    cookie_name: str = "access_token"


class CorsConfig(BaseModel):
    allowed_origins: list[str] = Field(default_factory=lambda: ["*"])
    allow_credentials: bool = True


class PaginationConfig(BaseModel):
    default_limit: int = 50
    max_limit: int = 1000


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="QUERY_", env_nested_delimiter="__")

    db: DatabaseConfig = Field(default_factory=DatabaseConfig)
    minio: MinioConfig = Field(default_factory=MinioConfig)
    jwt: JwtConfig = Field(default_factory=JwtConfig)
    cors: CorsConfig = Field(default_factory=CorsConfig)
    pagination: PaginationConfig = Field(default_factory=PaginationConfig)
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
