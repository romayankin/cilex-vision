"""Thin wrapper around the Docker SDK for container management.

Used by ServiceWatchdog (background auto-restart) and the /admin/services
router (manual ops UI). All Docker SDK calls are sync, so we wrap them in
``asyncio.to_thread()`` to avoid blocking the FastAPI event loop.

Diagnostics:
- VPN tunnel detection (Docker networking breaks under VPN routing)
- Disk space (containers can't restart if rootfs is full)
- Peer dependencies (don't restart X if its dep Y is down)
- Log pattern matching (DNS/OOM/permission/dep-refused/disk-full)
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import docker
from docker.errors import APIError, NotFound

logger = logging.getLogger(__name__)

COMPOSE_PROJECT_LABEL = "com.docker.compose.project"

# (regex, short label, resolution hint) — matched against recent log lines.
ERROR_PATTERNS: list[tuple[re.Pattern[str], str, str]] = [
    (
        re.compile(r"Temporary failure in name resolution", re.IGNORECASE),
        "DNS resolution failure",
        "Docker's internal DNS cannot resolve container names. Usually a VPN "
        "(AmneziaVPN, WireGuard, OpenVPN) is intercepting Docker traffic. "
        "Exclude Docker subnets (10.0.0.0/24, 10.10.0.0/24) from VPN routing.",
    ),
    (
        re.compile(r"OOM|Out of memory|Killed process", re.IGNORECASE),
        "Out of memory (OOM killed)",
        "The container was killed by the Linux OOM killer. Increase mem_limit "
        "in docker-compose.yml or reduce the workload.",
    ),
    (
        re.compile(r"Permission denied|EACCES", re.IGNORECASE),
        "Permission denied",
        "Cannot access a required file or directory. Check volume mounts and "
        "file ownership on the host.",
    ),
    (
        re.compile(r"Connection refused.*(?:5432|postgres|timescale)", re.IGNORECASE),
        "Database connection refused",
        "Cannot connect to TimescaleDB. Check the timescaledb container status.",
    ),
    (
        re.compile(r"Connection refused.*(?:9092|kafka)", re.IGNORECASE),
        "Kafka connection refused",
        "Cannot connect to a Kafka broker. Check kafka-0/kafka-1/kafka-2.",
    ),
    (
        re.compile(r"Connection refused.*(?:4222|nats)", re.IGNORECASE),
        "NATS connection refused",
        "Cannot connect to NATS. Check the nats container status.",
    ),
    (
        re.compile(r"NoSuchKey|NoSuchBucket|S3Error", re.IGNORECASE),
        "MinIO/S3 error",
        "Object storage operation failed. Check the minio container and bucket existence.",
    ),
    (
        re.compile(r"disk.*full|No space left on device|ENOSPC", re.IGNORECASE),
        "Disk full",
        "No disk space remaining. Run a storage purge from /admin/storage or "
        "free space manually (docker image prune, etc.).",
    ),
]

# container → list of containers it must have running to be usable.
DEPENDENCY_MAP: dict[str, list[str]] = {
    "inference-worker": ["kafka-0", "minio"],
    "decode-service": ["kafka-0", "minio"],
    "edge-agent": ["nats"],
    "ingress-bridge": ["nats", "kafka-0"],
    "event-engine": ["kafka-0", "timescaledb"],
    "clip-service": ["kafka-0", "minio", "timescaledb"],
    "bulk-collector": ["kafka-0", "timescaledb"],
    "query-api": ["timescaledb", "minio"],
    "frontend": ["query-api"],
    "attribute-service": ["kafka-0"],
    "mtmc-service": ["kafka-0", "redis"],
    "kafka-ui": ["kafka-0"],
    "grafana": ["prometheus"],
    "minio-init": ["minio"],
    "ollama-init": ["ollama"],
}


@dataclass
class ContainerInfo:
    name: str
    status: str  # running, exited, restarting, dead, paused, created
    health: str | None  # healthy, unhealthy, starting, None (no healthcheck)
    image: str
    started_at: str
    uptime_seconds: float
    exit_code: int | None
    restart_count: int


@dataclass
class DiagnosticResult:
    check: str
    status: str  # ok, warning, error
    message: str
    resolution: str = ""


def get_docker_client() -> docker.DockerClient:
    return docker.DockerClient(base_url="unix:///var/run/docker.sock")


async def list_containers(project: str | None = None) -> list[ContainerInfo]:
    """List all containers (optionally filtered by compose project label)."""

    def _list() -> list[ContainerInfo]:
        client = get_docker_client()
        try:
            filters: dict[str, Any] = {}
            if project:
                filters["label"] = f"{COMPOSE_PROJECT_LABEL}={project}"

            containers = client.containers.list(all=True, filters=filters)
            result: list[ContainerInfo] = []

            for c in containers:
                attrs = c.attrs or {}
                state = attrs.get("State", {})
                health_obj = state.get("Health")
                health_status = health_obj.get("Status") if health_obj else None

                started_at_str = state.get("StartedAt", "") or ""
                uptime = 0.0
                if started_at_str and state.get("Running"):
                    try:
                        started = datetime.fromisoformat(
                            started_at_str.replace("Z", "+00:00")
                        )
                        uptime = (
                            datetime.now(timezone.utc) - started
                        ).total_seconds()
                    except (ValueError, TypeError):
                        pass

                image_tag = ""
                try:
                    image_tag = c.image.tags[0] if c.image.tags else str(c.image.id)[:19]
                except Exception:
                    image_tag = "unknown"

                result.append(
                    ContainerInfo(
                        name=c.name,
                        status=c.status,
                        health=health_status,
                        image=image_tag,
                        started_at=started_at_str,
                        uptime_seconds=uptime,
                        exit_code=state.get("ExitCode"),
                        restart_count=attrs.get("RestartCount", 0),
                    )
                )

            result.sort(key=lambda x: x.name)
            return result
        finally:
            client.close()

    return await asyncio.to_thread(_list)


async def restart_container(name: str) -> tuple[bool, str]:
    """Restart a container by name. Returns (success, message)."""

    def _restart() -> tuple[bool, str]:
        client = get_docker_client()
        try:
            container = client.containers.get(name)
            container.restart(timeout=30)
            return True, f"Container '{name}' restarted"
        except NotFound:
            return False, f"Container '{name}' not found"
        except APIError as e:
            return False, f"Docker API error: {e}"
        finally:
            client.close()

    return await asyncio.to_thread(_restart)


async def stop_container(name: str, timeout: int = 10) -> tuple[bool, str]:
    """Stop a container by name. Returns (success, message)."""

    def _stop() -> tuple[bool, str]:
        client = get_docker_client()
        try:
            container = client.containers.get(name)
            container.stop(timeout=timeout)
            return True, f"Container '{name}' stopped"
        except NotFound:
            return False, f"Container '{name}' not found"
        except APIError as e:
            return False, f"Docker API error: {e}"
        finally:
            client.close()

    return await asyncio.to_thread(_stop)


async def get_container_logs(name: str, tail: int = 50) -> str:
    """Get recent log lines from a container."""

    def _logs() -> str:
        client = get_docker_client()
        try:
            container = client.containers.get(name)
            return container.logs(tail=tail, timestamps=True).decode(
                "utf-8", errors="replace"
            )
        except (NotFound, APIError) as e:
            return f"Error fetching logs: {e}"
        finally:
            client.close()

    return await asyncio.to_thread(_logs)


async def _check_vpn_interface() -> DiagnosticResult:
    # Note: this runs inside the query-api container, which uses Docker's bridge
    # net namespace — not the host's. So it can only see VPN interfaces if
    # iproute2 is installed AND the container shares the host network namespace
    # (it doesn't). Treat "ip not found" as ok-skip rather than error.
    try:
        proc = await asyncio.create_subprocess_exec(
            "ip", "addr", "show",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        output = stdout.decode()
    except FileNotFoundError:
        return DiagnosticResult(
            check="VPN tunnel",
            status="ok",
            message="VPN check skipped (iproute2 not installed in this container)",
        )
    except Exception as e:
        return DiagnosticResult(
            check="VPN tunnel",
            status="warning",
            message=f"Could not check VPN: {e}",
        )

    vpn_interfaces = re.findall(
        r"\d+:\s+(tun\d+|wg\d+|amnezia\w+):.*state\s+(\w+)", output
    )
    if not vpn_interfaces:
        return DiagnosticResult(
            check="VPN tunnel", status="ok", message="No VPN tunnel interfaces detected",
        )
    active = [(iface, st) for iface, st in vpn_interfaces if st == "UP"]
    if not active:
        return DiagnosticResult(
            check="VPN tunnel", status="ok",
            message=f"VPN interface(s) present but down: {', '.join(i for i, _ in vpn_interfaces)}",
        )
    return DiagnosticResult(
        check="VPN tunnel",
        status="warning",
        message=f"Active VPN interface(s): {', '.join(f'{i} ({s})' for i, s in active)}",
        resolution="VPN can intercept Docker networking. Ensure Docker subnets "
                   "(10.0.0.0/24, 10.10.0.0/24) are excluded from VPN routing "
                   "via split-tunnel configuration.",
    )


async def _check_disk_space() -> DiagnosticResult:
    try:
        proc = await asyncio.create_subprocess_exec(
            "df", "-h", "/",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        lines = stdout.decode().strip().split("\n")
    except Exception as e:
        return DiagnosticResult(
            check="Disk space", status="error", message=f"Could not check disk: {e}",
        )

    if len(lines) < 2:
        return DiagnosticResult(
            check="Disk space", status="error", message="df returned no rows",
        )
    parts = lines[1].split()
    try:
        use_pct = int(parts[4].rstrip("%"))
    except (IndexError, ValueError):
        return DiagnosticResult(
            check="Disk space", status="error", message=f"Could not parse df output: {lines[1]}",
        )

    if use_pct >= 95:
        return DiagnosticResult(
            check="Disk space", status="error",
            message=f"Disk usage at {use_pct}% — critically low",
            resolution="Free disk space immediately. Run a storage purge from "
                       "/admin/storage or 'docker image prune'.",
        )
    if use_pct >= 85:
        return DiagnosticResult(
            check="Disk space", status="warning",
            message=f"Disk usage at {use_pct}%",
            resolution="Consider running a storage purge from /admin/storage.",
        )
    return DiagnosticResult(check="Disk space", status="ok", message=f"Disk usage at {use_pct}%")


async def _check_dependencies(name: str) -> list[DiagnosticResult]:
    deps = DEPENDENCY_MAP.get(name, [])
    if not deps:
        return []
    containers = await list_containers()
    cmap = {c.name: c for c in containers}
    out: list[DiagnosticResult] = []
    for dep in deps:
        info = cmap.get(dep)
        if info is None:
            out.append(DiagnosticResult(
                check=f"Dependency: {dep}", status="error",
                message=f"Required container '{dep}' not found",
                resolution=f"Start '{dep}' first; '{name}' cannot function without it.",
            ))
        elif info.status != "running":
            out.append(DiagnosticResult(
                check=f"Dependency: {dep}", status="error",
                message=f"Required container '{dep}' is {info.status} (not running)",
                resolution=f"Restart '{dep}' first. Restarting '{name}' while its "
                           f"dependency is down has no chance of helping.",
            ))
        elif info.health == "unhealthy":
            out.append(DiagnosticResult(
                check=f"Dependency: {dep}", status="warning",
                message=f"Dependency '{dep}' is running but unhealthy",
                resolution=f"'{dep}' may be starting. Wait for it to become healthy "
                           f"before restarting '{name}'.",
            ))
        else:
            health_suffix = f" ({info.health})" if info.health else ""
            out.append(DiagnosticResult(
                check=f"Dependency: {dep}", status="ok",
                message=f"Dependency '{dep}' is running{health_suffix}",
            ))
    return out


async def _check_log_patterns(name: str) -> list[DiagnosticResult]:
    logs = await get_container_logs(name, tail=50)
    out: list[DiagnosticResult] = []
    matched: set[str] = set()
    for pattern, label, resolution in ERROR_PATTERNS:
        if pattern.search(logs) and label not in matched:
            matched.add(label)
            out.append(DiagnosticResult(
                check=f"Log analysis: {label}", status="error",
                message=f"Found '{label}' in recent logs",
                resolution=resolution,
            ))
    if not matched:
        out.append(DiagnosticResult(
            check="Log analysis", status="ok",
            message="No known error patterns found in recent logs",
        ))
    return out


async def run_diagnostics(name: str) -> list[DiagnosticResult]:
    """Run all diagnostic checks for a specific container."""
    results: list[DiagnosticResult] = []
    results.append(await _check_vpn_interface())
    results.append(await _check_disk_space())
    results.extend(await _check_dependencies(name))
    results.extend(await _check_log_patterns(name))
    return results


__all__ = [
    "ContainerInfo",
    "DiagnosticResult",
    "DEPENDENCY_MAP",
    "list_containers",
    "restart_container",
    "get_container_logs",
    "run_diagnostics",
]
