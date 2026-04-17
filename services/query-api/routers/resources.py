"""Admin-only endpoint that reports configured resource limits.

Reads limits directly from Docker's inspect API — what's actually applied,
not what a config file claims.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from auth.jwt import get_current_user
from schemas import UserClaims
from utils.docker_client import get_docker_client

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/resources", tags=["resources"])

# Host total for percentage calculation — override via env if needed.
HOST_MEM_BYTES = int(os.environ.get("HOST_MEM_BYTES", 16 * 1024 * 1024 * 1024))
HOST_CPU_COUNT = int(os.environ.get("HOST_CPU_COUNT", 20))


@router.get("/limits")
async def get_resource_limits(
    user: UserClaims = Depends(get_current_user),
) -> dict[str, Any]:
    """Return per-container memory and CPU limits from Docker inspect."""
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin only")

    try:
        client = get_docker_client()
    except Exception as exc:
        logger.warning("Docker client unavailable: %s", exc)
        raise HTTPException(status_code=503, detail="Docker API unavailable")

    services: list[dict[str, Any]] = []
    total_mem_bytes = 0
    total_cpu = 0.0

    try:
        for container in client.containers.list(all=True):
            try:
                info = container.attrs
                name = container.name
                host_config = info.get("HostConfig", {})

                mem_bytes = int(host_config.get("Memory") or 0)
                memswap_bytes = int(host_config.get("MemorySwap") or 0)
                nano_cpus = int(host_config.get("NanoCpus") or 0)
                cpus = nano_cpus / 1e9 if nano_cpus else 0.0

                state = info.get("State", {}).get("Status", "unknown")

                services.append({
                    "name": name,
                    "state": state,
                    "mem_limit_bytes": mem_bytes,
                    "mem_limit_mb": round(mem_bytes / (1024 * 1024), 1) if mem_bytes else None,
                    "memswap_limit_bytes": memswap_bytes,
                    "memswap_limit_mb": round(memswap_bytes / (1024 * 1024), 1) if memswap_bytes else None,
                    "swap_allowed_mb": round(max(0, memswap_bytes - mem_bytes) / (1024 * 1024), 1) if memswap_bytes and mem_bytes else None,
                    "cpus": round(cpus, 2) if cpus else None,
                    "has_mem_limit": mem_bytes > 0,
                    "has_cpu_limit": cpus > 0,
                })

                if mem_bytes:
                    total_mem_bytes += mem_bytes
                if cpus:
                    total_cpu += cpus
            except Exception:
                logger.warning("Failed to read resources for container", exc_info=True)
                continue
    finally:
        client.close()

    services.sort(key=lambda s: s["name"])

    unlimited_count = sum(1 for s in services if not s["has_mem_limit"])

    return {
        "host": {
            "total_mem_bytes": HOST_MEM_BYTES,
            "total_mem_gb": round(HOST_MEM_BYTES / (1024**3), 1),
            "total_cpus": HOST_CPU_COUNT,
        },
        "totals": {
            "mem_allocated_bytes": total_mem_bytes,
            "mem_allocated_gb": round(total_mem_bytes / (1024**3), 2),
            "mem_allocated_pct": round(total_mem_bytes / HOST_MEM_BYTES * 100, 1),
            "cpu_allocated": round(total_cpu, 1),
            "cpu_allocated_pct": round(total_cpu / HOST_CPU_COUNT * 100, 1),
            "services_with_limits": len(services) - unlimited_count,
            "services_without_limits": unlimited_count,
            "total_services": len(services),
        },
        "services": services,
    }
