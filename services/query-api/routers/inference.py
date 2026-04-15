"""Admin-only proxy for the inference worker's JSON metrics endpoint.

The inference worker exposes its Prometheus metrics in JSON form at
``http://inference-worker:9091/metrics/json`` for the admin dashboard at
``/admin/inference``. Prometheus-format metrics on port 9090 are still
the source of truth for Grafana; this JSON view is a convenience for the
UI so it can poll from the browser without parsing Prom text.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request

from auth.jwt import get_current_user
from schemas import UserClaims

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/inference", tags=["inference"])

INFERENCE_METRICS_URL = os.environ.get(
    "INFERENCE_METRICS_URL",
    "http://inference-worker:9091/metrics/json",
)
INFERENCE_METRICS_TIMEOUT_S = 5.0


@router.get("/metrics")
async def inference_metrics(
    request: Request,
    user: UserClaims = Depends(get_current_user),
) -> dict[str, Any]:
    """Return current inference-worker metrics for the admin dashboard."""
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin only")

    try:
        async with httpx.AsyncClient(timeout=INFERENCE_METRICS_TIMEOUT_S) as client:
            resp = await client.get(INFERENCE_METRICS_URL)
    except httpx.HTTPError as exc:
        logger.warning("Inference metrics fetch failed: %s", exc)
        raise HTTPException(
            status_code=502, detail="Inference worker metrics unavailable"
        )

    if resp.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"Inference worker returned HTTP {resp.status_code}",
        )

    return resp.json()
