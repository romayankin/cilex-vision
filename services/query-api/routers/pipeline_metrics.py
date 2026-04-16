"""Pipeline metrics + Kafka queue ops for the Activity Monitor dashboard.

Two endpoints:
  - GET  /pipeline/metrics                 — point-in-time scrape of all 7
    pipeline services, returns raw counters/gauges grouped by service so the
    frontend can compute rates by differencing successive polls.
  - POST /pipeline/kafka/purge/{group}     — reset offsets to latest for one
    consumer group (drops unprocessed messages). Audit-logged.
  - POST /pipeline/kafka/purge-all         — same, for every known group.

Raw counters (not rates) are returned because counters reset on container
restart; differencing in the browser handles that cleanly while server-side
rates would jump to negative or 'huge' on restart and require special-casing.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request

from auth.audit import _client_hostname, _client_ip, _write_audit_log
from auth.jwt import get_current_user
from schemas import UserClaims

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/pipeline", tags=["pipeline"])

SCRAPE_TIMEOUT_S = 3.0

# (service_key, scrape_url, [metric_prefixes_to_extract])
SERVICE_SCRAPE_TARGETS: list[tuple[str, str, list[str]]] = [
    (
        "edge_agent",
        "http://edge-agent:9090/metrics",
        [
            "edge_motion_frames_total",
            "edge_static_frames_filtered_total",
            "edge_camera_uptime_ratio",
            "edge_nats_publish_latency_ms_count",
            "edge_nats_publish_latency_ms_sum",
        ],
    ),
    (
        "ingress_bridge",
        "http://ingress-bridge:8080/metrics",
        [
            "bridge_messages_received_total",
            "bridge_messages_produced_total",
            "bridge_messages_spooled_total",
            "bridge_spool_depth_messages",
            "bridge_nats_consumer_lag",
        ],
    ),
    (
        "decode_service",
        "http://decode-service:9090/metrics",
        [
            "decode_frames_consumed_total",
            "decode_frames_decoded_total",
            "decode_frames_sampled_total",
            "decode_frames_skipped_total",
            "decode_latency_ms_count",
            "decode_latency_ms_sum",
            "decode_consumer_lag",
        ],
    ),
    (
        "inference_worker",
        "http://inference-worker:9090/metrics",
        [
            "inference_frames_consumed_total",
            "inference_latency_ms_count",
            "inference_latency_ms_sum",
            "inference_embedding_latency_ms_count",
            "inference_embedding_latency_ms_sum",
            "inference_detections_total",
            "inference_consumer_lag",
        ],
    ),
    (
        "event_engine",
        "http://event-engine:8080/metrics",
        [
            "event_tracklets_consumed_total",
            "event_emitted_total",
            "event_active_state_machines",
        ],
    ),
    (
        "clip_service",
        "http://clip-service:8080/metrics",
        [
            "clip_extracted_total",
            "clip_events_consumed_total",
            "clip_events_skipped_total",
            "clip_extraction_errors_total",
        ],
    ),
    (
        "bulk_collector",
        "http://bulk-collector:8080/metrics",
        [
            "bulk_rows_staged",
            "bulk_consumer_lag",
            "bulk_rows_written_total",
        ],
    ),
]


# Consumer group name → topic it consumes (informational; not used for reset).
KAFKA_CONSUMER_GROUPS: dict[str, dict[str, str]] = {
    "decode-worker": {
        "topic": "frames.sampled.refs",
        "label": "Decode queue",
    },
    "detector-worker": {
        "topic": "frames.decoded.refs",
        "label": "Inference queue",
    },
    "bulk-collector-detections": {
        "topic": "bulk.detections",
        "label": "DB write queue",
    },
    "event-engine": {
        "topic": "tracklets.local",
        "label": "Event queue",
    },
    "clip-service": {
        "topic": "events.raw",
        "label": "Clip queue",
    },
}


async def _scrape(
    client: httpx.AsyncClient, url: str, prefixes: list[str]
) -> dict[str, Any]:
    """Fetch a Prometheus /metrics endpoint and pluck out matching series.

    Each line is keyed by the full series identifier (`metric{labels}`) so
    multi-label counters (e.g. `inference_detections_total{class="person"}`)
    don't collapse into one value.
    """
    try:
        resp = await client.get(url)
    except httpx.HTTPError as exc:
        return {"error": f"unreachable: {exc.__class__.__name__}"}
    if resp.status_code != 200:
        return {"error": f"HTTP {resp.status_code}"}

    out: dict[str, float] = {}
    for line in resp.text.splitlines():
        if not line or line.startswith("#"):
            continue
        for prefix in prefixes:
            if line.startswith(prefix):
                parts = line.split()
                if len(parts) < 2:
                    break
                try:
                    out[parts[0]] = float(parts[1])
                except ValueError:
                    pass
                break
    return out


@router.get("/metrics")
async def pipeline_metrics(
    request: Request,
    user: UserClaims = Depends(get_current_user),
) -> dict[str, Any]:
    """Returns a point-in-time snapshot of all pipeline service metrics."""
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin only")

    async with httpx.AsyncClient(timeout=SCRAPE_TIMEOUT_S) as client:
        results = await asyncio.gather(
            *(_scrape(client, url, prefixes) for _, url, prefixes in SERVICE_SCRAPE_TARGETS)
        )

    services = {
        key: data
        for (key, _, _), data in zip(SERVICE_SCRAPE_TARGETS, results, strict=True)
    }

    return {"ts": time.time(), "services": services}


# ----------------------------------------------------------------------
# Kafka queue purge
# ----------------------------------------------------------------------


def _purge_consumer_group(group_name: str) -> dict[str, Any]:
    """Reset a single consumer group's offsets to latest via kafka-consumer-groups.sh.

    Runs synchronously inside an exec into the kafka-0 container. Caller
    should wrap in `asyncio.to_thread()`. Reports honestly on failure
    (the consumer group must be inactive — for live consumers this command
    fails and we surface that error in the response).
    """
    import docker as docker_sdk  # noqa: PLC0415

    client = docker_sdk.DockerClient(base_url="unix:///var/run/docker.sock")
    try:
        container = client.containers.get("kafka-0")
        cmd = (
            "/opt/bitnami/kafka/bin/kafka-consumer-groups.sh "
            "--bootstrap-server localhost:9092 "
            f"--group {group_name} "
            "--reset-offsets --to-latest --execute --all-topics"
        )
        exit_code, output = container.exec_run(cmd, demux=True)
        stdout = output[0].decode() if output and output[0] else ""
        stderr = output[1].decode() if output and output[1] else ""
        return {
            "group": group_name,
            "success": exit_code == 0,
            "output": stdout[:1000],
            "error": stderr[:1000] if exit_code != 0 else None,
        }
    except Exception as exc:
        return {"group": group_name, "success": False, "error": str(exc)}
    finally:
        client.close()


async def _audit_purge(
    request: Request,
    user: UserClaims,
    group_name: str,
    result: dict[str, Any],
) -> None:
    pool = getattr(request.app.state, "db_pool", None)
    if pool is None:
        return
    try:
        await _write_audit_log(
            pool=pool,
            user_id=user.user_id,
            action="KAFKA_QUEUE_PURGE",
            resource_type="kafka",
            resource_id=group_name,
            details={
                "username": user.username,
                "group": group_name,
                "success": result.get("success", False),
                "output": result.get("output", "")[:500],
                "error": (result.get("error") or "")[:500],
            },
            ip_address=_client_ip(request),
            hostname=_client_hostname(request),
        )
    except Exception:
        logger.warning("Audit log for Kafka purge failed", exc_info=True)
    request.state.audit_written = True


@router.post("/kafka/purge/{group_name}")
async def purge_kafka_queue(
    group_name: str,
    request: Request,
    user: UserClaims = Depends(get_current_user),
) -> dict[str, Any]:
    """Drop all unprocessed messages for one consumer group.

    Emergency operation — the consumer group must be inactive for the reset
    to succeed (kafka-consumer-groups.sh refuses to mutate an active group).
    Returns the kafka tool's stdout/stderr so the operator can see exactly
    what happened.
    """
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin only")

    if group_name not in KAFKA_CONSUMER_GROUPS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unknown consumer group: {group_name}. "
                f"Allowed: {', '.join(sorted(KAFKA_CONSUMER_GROUPS))}"
            ),
        )

    result = await asyncio.to_thread(_purge_consumer_group, group_name)
    await _audit_purge(request, user, group_name, result)

    if not result["success"]:
        raise HTTPException(
            status_code=500,
            detail=f"Purge failed: {result.get('error') or 'unknown error'}",
        )

    return result


@router.post("/kafka/purge-all")
async def purge_all_kafka_queues(
    request: Request,
    user: UserClaims = Depends(get_current_user),
) -> dict[str, Any]:
    """Purge every known consumer group. Nuclear option."""
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin only")

    groups = sorted(KAFKA_CONSUMER_GROUPS.keys())
    results = await asyncio.gather(
        *(asyncio.to_thread(_purge_consumer_group, g) for g in groups)
    )
    by_group = {r["group"]: r for r in results}

    for group_name, result in by_group.items():
        await _audit_purge(request, user, group_name, result)

    return {
        "purged": by_group,
        "all_succeeded": all(r["success"] for r in results),
    }


@router.get("/kafka/groups")
async def list_kafka_groups(
    user: UserClaims = Depends(get_current_user),
) -> dict[str, Any]:
    """Static metadata about consumer groups for the UI to render purge controls."""
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    return {"groups": KAFKA_CONSUMER_GROUPS}
