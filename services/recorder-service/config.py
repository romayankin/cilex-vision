"""Configuration for the continuous recorder service."""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings


class RecorderSettings(BaseSettings):
    db_dsn: str = Field(
        "postgresql://cilex:cilex_dev_password@timescaledb:5432/vidanalytics",
        env="RECORDER_DB_DSN",
    )

    minio_url: str = Field("minio:9000", env="RECORDER_MINIO_URL")
    minio_access_key: str = Field("minioadmin", env="RECORDER_MINIO_ACCESS_KEY")
    minio_secret_key: str = Field("minioadmin123", env="RECORDER_MINIO_SECRET_KEY")
    minio_secure: bool = Field(False, env="RECORDER_MINIO_SECURE")
    bucket_hot: str = Field("raw-video-hot", env="RECORDER_BUCKET_HOT")

    go2rtc_base: str = Field("rtsp://go2rtc:8554", env="RECORDER_GO2RTC_BASE")

    segment_duration_s: int = Field(30, env="RECORDER_SEGMENT_DURATION_S")
    segment_format: str = Field("mpegts", env="RECORDER_SEGMENT_FORMAT")

    work_dir: str = Field("/tmp/recorder", env="RECORDER_WORK_DIR")

    health_port: int = Field(8081, env="RECORDER_HEALTH_PORT")
    metrics_port: int = Field(9090, env="RECORDER_METRICS_PORT")

    log_level: str = Field("INFO", env="RECORDER_LOG_LEVEL")

    class Config:
        env_prefix = "RECORDER_"
