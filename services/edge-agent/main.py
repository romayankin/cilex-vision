"""Edge Agent — entry point.

Loads configuration from YAML, connects to NATS and MinIO, starts one
``CameraPipeline`` per enabled camera, and runs until SIGINT / SIGTERM.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import time
from typing import Any

from minio import Minio
from prometheus_client import start_http_server

from camera_pipeline import CameraPipeline
from config import Settings
from local_buffer import LocalBuffer
from nats_publisher import NatsPublisher

logger = logging.getLogger(__name__)


async def _start_health_server(
    port: int,
    started_at: float,
    nats_pub: NatsPublisher,
    pipelines: list[CameraPipeline],
) -> Any:
    try:
        from aiohttp import web  # noqa: PLC0415
    except ImportError:
        logger.warning("aiohttp not installed — /health endpoint disabled")
        return None

    async def health_handler(_request: Any) -> Any:
        now = time.time()
        now_mono = time.monotonic()
        uptime = now - started_at
        checks: dict[str, Any] = {}
        healthy = True

        if nats_pub.is_connected:
            checks["nats"] = "connected"
        else:
            checks["nats"] = "disconnected"
            healthy = False

        active = 0
        stale: list[str] = []
        for p in pipelines:
            last = getattr(p, "_last_frame_time", 0.0)
            if last == 0.0:
                continue
            age = now_mono - last
            if age < 60:
                active += 1
            else:
                stale.append(f"{p._camera.camera_id} ({int(age)}s)")

        if pipelines and active > 0:
            checks["cameras"] = f"{active}/{len(pipelines)} active"
        elif pipelines and uptime > 120:
            checks["cameras"] = (
                f"all stale: {', '.join(stale)}" if stale else "no frames yet"
            )
            healthy = False
        else:
            checks["cameras"] = "warming up"

        body = {
            "status": "ok" if healthy else "unhealthy",
            "uptime_seconds": int(uptime),
            "checks": checks,
        }
        return web.json_response(body, status=200 if healthy else 503)

    app = web.Application()
    app.router.add_get("/health", health_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info("Health server on port %d", port)
    return runner


async def run(settings: Settings) -> None:
    """Async entry point — sets up all components and runs pipelines."""

    started_at = time.time()

    # --- Prometheus metrics server ---
    start_http_server(settings.metrics_port)
    logger.info("Prometheus metrics at :%d/metrics", settings.metrics_port)

    # --- MinIO client ---
    minio_client = Minio(
        settings.minio.endpoint,
        access_key=settings.minio.access_key,
        secret_key=settings.minio.secret_key,
        secure=settings.minio.secure,
    )
    if not minio_client.bucket_exists(settings.minio.bucket):
        minio_client.make_bucket(settings.minio.bucket)
        logger.info("Created MinIO bucket: %s", settings.minio.bucket)

    # --- NATS JetStream publisher ---
    nats_pub = NatsPublisher(
        url=settings.nats.url,
        site_id=settings.site_id,
        cert_file=settings.nats.tls.cert_file if settings.nats.tls else None,
        key_file=settings.nats.tls.key_file if settings.nats.tls else None,
        ca_file=settings.nats.tls.ca_file if settings.nats.tls else None,
    )
    await nats_pub.connect()

    # --- Local ring buffer (shared across cameras) ---
    buffer = LocalBuffer(
        path=settings.buffer.path,
        max_bytes=settings.buffer.max_bytes,
        replay_rate_limit=settings.buffer.replay_rate_limit,
    )

    # --- Camera pipelines ---
    pipelines: list[CameraPipeline] = []
    tasks: list[asyncio.Task[None]] = []

    for cam in settings.cameras:
        if not cam.enabled:
            continue
        pipeline = CameraPipeline(
            camera=cam,
            site_id=settings.site_id,
            nats_pub=nats_pub,
            minio_client=minio_client,
            minio_cfg=settings.minio,
            motion_cfg=settings.motion,
            buffer=buffer,
        )
        pipelines.append(pipeline)
        tasks.append(
            asyncio.create_task(pipeline.run(), name=f"cam-{cam.camera_id}")
        )

    logger.info("Started %d camera pipeline(s)", len(tasks))

    # --- Health server ---
    health_runner = await _start_health_server(
        settings.health_port, started_at, nats_pub, pipelines
    )

    # --- Graceful shutdown ---
    shutdown_event = asyncio.Event()

    def _signal_handler() -> None:
        logger.info("Shutdown signal received")
        for p in pipelines:
            p.shutdown()
        shutdown_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _signal_handler)

    await shutdown_event.wait()
    await asyncio.gather(*tasks, return_exceptions=True)
    if health_runner is not None:
        try:
            await health_runner.cleanup()
        except Exception:
            pass
    await nats_pub.close()
    logger.info("Edge agent stopped")


def main() -> None:
    config_path = os.environ.get("EDGE_CONFIG", "config.yaml")
    settings = Settings.from_yaml(config_path)

    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format=(
            '{"time":"%(asctime)s","level":"%(levelname)s",'
            '"logger":"%(name)s","message":"%(message)s"}'
        ),
    )
    logger.info(
        "Edge Agent starting — site=%s cameras=%d",
        settings.site_id,
        len(settings.cameras),
    )

    asyncio.run(run(settings))


if __name__ == "__main__":
    main()
