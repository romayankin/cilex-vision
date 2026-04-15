"""Basic Query API for the Cilex Vision platform.

FastAPI application providing read-only access to detections, tracks,
and events stored in TimescaleDB / PostgreSQL.

- JWT auth via httpOnly cookies
- RBAC with 4 roles: admin, operator, viewer, engineering
- Camera scope filtering per user
- Audit logging of every request to audit_logs table
- Signed MinIO URLs for clip/thumbnail access
- Prometheus metrics at /metrics
"""

from __future__ import annotations

import argparse
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import make_asgi_app

from auth.audit import AuditMiddleware
from config import Settings
from storage_watchdog import StorageWatchdog
from routers import (
    audit as audit_router,
    auth,
    debug,
    detections,
    discovery,
    events,
    lpr,
    pipeline,
    settings as settings_router,
    similarity,
    storage,
    streams,
    topology,
    tracks,
)
from routers.streams import sync_all_to_go2rtc
from utils.db import create_pool
from utils.minio_urls import create_minio_client

logger = logging.getLogger(__name__)

DEFAULT_CONFIG = Path(__file__).resolve().parent / "config.yaml"


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup: create DB pool and MinIO client. Shutdown: close pool."""
    settings: Settings = app.state.settings

    # asyncpg pool
    pool = await create_pool(
        dsn=settings.db.dsn,
        min_size=settings.db.min_pool_size,
        max_size=settings.db.max_pool_size,
        command_timeout=settings.db.command_timeout_s,
    )
    app.state.db_pool = pool
    logger.info("Database pool created")

    # MinIO client for signed URLs
    app.state.minio_client = create_minio_client(
        endpoint=settings.minio.endpoint,
        access_key=settings.minio.access_key,
        secret_key=settings.minio.secret_key,
        secure=settings.minio.secure,
    )

    # Storage quota watchdog
    quota_percent = int(os.environ.get("STORAGE_QUOTA_PERCENT", "50"))
    watchdog = StorageWatchdog(
        app.state.minio_client,
        quota_percent=quota_percent,
        db_pool=pool,
    )
    app.state.storage_watchdog = watchdog
    await watchdog.start()

    # Register all DB cameras with go2rtc so streams survive a go2rtc restart.
    try:
        synced = await sync_all_to_go2rtc(pool)
        logger.info("go2rtc sync registered %d cameras", synced)
    except Exception as exc:  # noqa: BLE001 — sync is best-effort at startup
        logger.warning("go2rtc startup sync skipped: %s", exc)

    yield

    # Shutdown
    await watchdog.stop()
    if pool is not None:
        await pool.close()
        logger.info("Database pool closed")


def create_app(settings: Settings | None = None) -> FastAPI:
    """Create and configure the FastAPI application."""
    if settings is None:
        settings = Settings()

    app = FastAPI(
        title="Cilex Vision Query API",
        version="1.0.0",
        description="Read-only API for detections, tracks, and events.",
        lifespan=lifespan,
    )

    app.state.settings = settings

    # CORS
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors.allowed_origins,
        allow_credentials=settings.cors.allow_credentials,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["*"],
    )

    # Audit logging middleware
    app.add_middleware(AuditMiddleware)

    # Routers
    app.include_router(auth.router)
    app.include_router(detections.router)
    app.include_router(tracks.router)
    app.include_router(events.router)
    app.include_router(lpr.router)
    app.include_router(debug.router)
    app.include_router(topology.router)
    app.include_router(similarity.router)
    app.include_router(streams.router)
    app.include_router(discovery.router)
    app.include_router(pipeline.router)
    app.include_router(storage.router)
    app.include_router(settings_router.router)
    app.include_router(audit_router.router)

    # Prometheus metrics
    metrics_app = make_asgi_app()
    app.mount("/metrics", metrics_app)

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    @app.get("/ready")
    async def ready() -> dict:
        pool = getattr(app.state, "db_pool", None)
        if pool is None:
            return {"status": "not_ready", "reason": "no_db_pool"}
        try:
            async with pool.acquire() as conn:
                await conn.execute("SELECT 1")
            return {"status": "ready"}
        except Exception:
            return {"status": "not_ready", "reason": "db_unreachable"}

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config", type=Path, default=DEFAULT_CONFIG, help="YAML config path."
    )
    parser.add_argument("--host", default="0.0.0.0", help="Bind address.")
    parser.add_argument("--port", type=int, default=8000, help="Bind port.")
    args = parser.parse_args()

    settings = Settings.from_yaml(args.config)
    setup_logging(settings.log_level)

    app = create_app(settings)

    try:
        import uvicorn  # noqa: PLC0415
    except ImportError as exc:
        raise RuntimeError(
            "missing optional dependency 'uvicorn'; install requirements.txt"
        ) from exc

    logger.info("Starting Query API on %s:%d", args.host, args.port)
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        raise SystemExit(str(exc)) from exc
