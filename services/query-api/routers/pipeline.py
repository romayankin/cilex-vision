"""GET /pipeline/status — real-time pipeline health aggregator.

Scrapes Prometheus /metrics endpoints on every pipeline service and combines
them with DB counts so the admin UI can render a single live diagram of the
pipeline from edge to DB.

Admin-only. Individual scrape failures are reported inline on the stage
({"error": "..."}) rather than failing the whole request — a healthy
backend should keep responding even when one worker is down.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request

from auth.jwt import get_current_user
from schemas import UserClaims

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/pipeline", tags=["pipeline"])

SCRAPE_TIMEOUT_S = 3.0

# (container_name, port, [metric_name_prefixes_to_extract])
EDGE_METRICS = [
    "edge_motion_frames_total",
    "edge_static_frames_filtered_total",
    "edge_camera_uptime_ratio",
    "edge_nats_publish_latency_ms_count",
    "edge_buffer_fill_bytes",
]
BRIDGE_METRICS = [
    "bridge_messages_received_total",
    "bridge_messages_produced_total",
    "bridge_messages_spooled_total",
    "bridge_spool_depth_messages",
    "bridge_nats_consumer_lag",
]
DECODE_METRICS = [
    "decode_frames_consumed_total",
    "decode_frames_decoded_total",
    "decode_frames_sampled_total",
    "decode_frames_skipped_total",
    "decode_latency_ms_count",
    "decode_latency_ms_sum",
    "decode_consumer_lag",
]
INFERENCE_METRICS = [
    "inference_frames_consumed_total",
    "inference_latency_ms_count",
    "inference_latency_ms_sum",
    "inference_embedding_latency_ms_count",
]
BULK_METRICS = [
    "bulk_rows_staged",
    "bulk_consumer_lag",
]

# Per-stage scrape target list: name, URL, metric prefixes, container name
STAGES: list[tuple[str, str, list[str], str]] = [
    ("edge_agent", "http://edge-agent:9090/metrics", EDGE_METRICS, "edge-agent"),
    ("ingress_bridge", "http://ingress-bridge:8080/metrics", BRIDGE_METRICS, "ingress-bridge"),
    ("decode_service", "http://decode-service:9090/metrics", DECODE_METRICS, "decode-service"),
    ("inference_worker", "http://inference-worker:9090/metrics", INFERENCE_METRICS, "inference-worker"),
    ("bulk_collector", "http://bulk-collector:8080/metrics", BULK_METRICS, "bulk-collector"),
]


async def _scrape_metrics(
    client: httpx.AsyncClient,
    url: str,
    metric_names: list[str],
) -> dict[str, Any]:
    """Fetch a Prometheus /metrics endpoint and extract named series.

    Returns a dict keyed by the full series line (metric + labels) so that
    multi-label counters (e.g. per-camera) don't overwrite each other.
    """
    try:
        resp = await client.get(url)
    except httpx.HTTPError as exc:
        return {"error": f"unreachable: {exc.__class__.__name__}"}
    if resp.status_code != 200:
        return {"error": f"HTTP {resp.status_code}"}

    metrics: dict[str, float] = {}
    for line in resp.text.splitlines():
        if not line or line.startswith("#"):
            continue
        for name in metric_names:
            if line.startswith(name):
                parts = line.split()
                if len(parts) < 2:
                    break
                try:
                    metrics[parts[0]] = float(parts[1])
                except ValueError:
                    pass
                break
    return metrics


async def _container_up(client: httpx.AsyncClient, url: str) -> str:
    try:
        resp = await client.get(url)
    except httpx.HTTPError:
        return "down"
    return "up" if resp.status_code == 200 else "error"


@router.get("/status")
async def pipeline_status(
    request: Request,
    user: UserClaims = Depends(get_current_user),
) -> dict[str, Any]:
    """Aggregate Prometheus metrics + DB counts into a single status blob."""
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin only")

    pool = request.app.state.db_pool

    async with httpx.AsyncClient(timeout=SCRAPE_TIMEOUT_S) as client:
        # Scrape every stage in parallel — one unreachable service shouldn't
        # delay the whole dashboard by SCRAPE_TIMEOUT_S * N seconds.
        scrape_results = await asyncio.gather(
            *(_scrape_metrics(client, url, names) for _, url, names, _ in STAGES)
        )
        container_results = await asyncio.gather(
            *(_container_up(client, url) for _, url, _, _ in STAGES)
        )

    stages: dict[str, dict[str, Any]] = {}
    containers: dict[str, str] = {}
    for (stage_key, _, _, container_name), metrics, status in zip(
        STAGES, scrape_results, container_results, strict=True
    ):
        stages[stage_key] = metrics
        containers[container_name] = status

    async with pool.acquire() as conn:
        det_count = await conn.fetchval("SELECT COUNT(*) FROM detections") or 0
        det_recent = await conn.fetchval(
            "SELECT COUNT(*) FROM detections WHERE time > NOW() - INTERVAL '5 minutes'"
        ) or 0
        track_count = await conn.fetchval("SELECT COUNT(*) FROM local_tracks") or 0
        active_tracks = await conn.fetchval(
            "SELECT COUNT(*) FROM local_tracks WHERE state = 'active'"
        ) or 0
        event_count = await conn.fetchval("SELECT COUNT(*) FROM events") or 0
        latest_det = await conn.fetchval("SELECT MAX(time) FROM detections")

        per_camera = await conn.fetch(
            "SELECT c.camera_id, c.name, c.status, "
            "  (SELECT COUNT(*) FROM detections d "
            "     WHERE d.camera_id = c.camera_id "
            "       AND d.time > NOW() - INTERVAL '5 minutes') AS det_5min, "
            "  (SELECT COUNT(*) FROM local_tracks t "
            "     WHERE t.camera_id = c.camera_id) AS track_total "
            "FROM cameras c ORDER BY c.camera_id"
        )

    return {
        "stages": stages,
        "containers": containers,
        "database": {
            "total_detections": det_count,
            "detections_last_5min": det_recent,
            "total_tracks": track_count,
            "active_tracks": active_tracks,
            "total_events": event_count,
            "latest_detection": latest_det.isoformat() if latest_det else None,
        },
        "cameras": [
            {
                "camera_id": r["camera_id"],
                "name": r["name"],
                "status": r["status"],
                "detections_5min": r["det_5min"],
                "total_tracks": r["track_total"],
            }
            for r in per_camera
        ],
    }
