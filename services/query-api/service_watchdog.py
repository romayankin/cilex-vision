"""Background watchdog that monitors container health and auto-restarts.

Runs as an asyncio task inside query-api. Polls Docker every 30s and
auto-restarts unhealthy containers with escalating backoff:
  Attempt 1: immediate, then wait 30s before re-checking
  Attempt 2: wait 1 min before re-checking
  Attempt 3: wait 5 min before re-checking
After 3 failures: marked FAILED, diagnostics run and stored, restarts stop.

All restart attempts (auto and manual) are logged to ``audit_logs``.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from auth.audit import _write_audit_log
from utils.docker_client import (
    DEPENDENCY_MAP,
    list_containers,
    restart_container,
    run_diagnostics,
)

logger = logging.getLogger(__name__)

POLL_INTERVAL_S = 30
BACKOFF_SCHEDULE = [30, 60, 300]  # seconds — wait AFTER each attempt before re-checking

# Containers we should never auto-restart (one-shot init jobs).
IGNORE_CONTAINERS: set[str] = {"minio-init"}
# Containers that are expected to exit cleanly after their work is done.
ONESHOT_CONTAINERS: set[str] = {"minio-init"}


@dataclass
class RestartState:
    attempt: int = 0
    last_attempt_at: datetime | None = None
    next_retry_at: datetime | None = None
    failed: bool = False
    diagnostics: list[dict[str, Any]] = field(default_factory=list)


class ServiceWatchdog:
    """Polls Docker, auto-restarts unhealthy containers with backoff."""

    def __init__(self, db_pool: Any) -> None:
        self._pool = db_pool
        self._restart_states: dict[str, RestartState] = {}
        self._task: asyncio.Task[None] | None = None
        self._running = False

    async def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._loop())
        logger.info("ServiceWatchdog started (poll every %ds)", POLL_INTERVAL_S)

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        logger.info("ServiceWatchdog stopped")

    def get_restart_states(self) -> dict[str, dict[str, Any]]:
        """Return the current restart-tracking snapshot for the API."""
        out: dict[str, dict[str, Any]] = {}
        for name, st in self._restart_states.items():
            out[name] = {
                "attempt": st.attempt,
                "max_attempts": len(BACKOFF_SCHEDULE),
                "last_attempt_at": st.last_attempt_at.isoformat() if st.last_attempt_at else None,
                "next_retry_at": st.next_retry_at.isoformat() if st.next_retry_at else None,
                "failed": st.failed,
                "diagnostics": st.diagnostics,
            }
        return out

    def clear_restart_state(self, name: str) -> None:
        """Drop watchdog tracking for a container — called after manual restart."""
        self._restart_states.pop(name, None)

    async def _loop(self) -> None:
        # Let containers settle before the first poll (avoids spurious "down" right after compose up).
        await asyncio.sleep(10)
        while self._running:
            try:
                await self._check_containers()
            except Exception:
                logger.exception("Watchdog poll error")
            await asyncio.sleep(POLL_INTERVAL_S)

    async def _check_containers(self) -> None:
        containers = await list_containers()
        cmap = {c.name: c for c in containers}
        now = datetime.now(timezone.utc)

        for c in containers:
            if c.name in IGNORE_CONTAINERS:
                continue
            if c.name in ONESHOT_CONTAINERS and c.status == "exited" and c.exit_code == 0:
                continue

            is_down = c.status in ("exited", "dead", "restarting") or (
                c.status == "running" and c.health == "unhealthy"
            )

            if not is_down:
                # Recovered (or never broken) — clear tracking.
                state = self._restart_states.get(c.name)
                if state and state.attempt > 0:
                    logger.info(
                        "Container '%s' recovered after %d attempt(s)",
                        c.name, state.attempt,
                    )
                    await self._write_audit(
                        c.name, "auto", True,
                        f"Recovered after {state.attempt} attempt(s)",
                    )
                    del self._restart_states[c.name]
                continue

            state = self._restart_states.setdefault(c.name, RestartState())

            if state.failed:
                continue
            if state.next_retry_at and now < state.next_retry_at:
                continue

            if state.attempt >= len(BACKOFF_SCHEDULE):
                # Exhausted retries — diagnose and mark failed.
                logger.warning(
                    "Container '%s' failed after %d attempts — running diagnostics",
                    c.name, state.attempt,
                )
                diag = await run_diagnostics(c.name)
                state.failed = True
                state.diagnostics = [
                    {"check": d.check, "status": d.status,
                     "message": d.message, "resolution": d.resolution}
                    for d in diag
                ]
                non_ok = [d for d in diag if d.status != "ok"]
                summary = (
                    "; ".join(f"{d.check}: {d.message}" for d in non_ok)
                    if non_ok else "no diagnostic issues identified"
                )
                await self._write_audit(
                    c.name, "auto", False,
                    f"Failed after {state.attempt} attempts. Diagnostics: {summary}",
                )
                continue

            # Skip if a hard dependency is down — restarting now would just fail.
            deps = DEPENDENCY_MAP.get(c.name, [])
            deps_down = [
                d for d in deps
                if cmap.get(d) is None or cmap[d].status != "running"
            ]
            if deps_down:
                logger.info(
                    "Skipping restart of '%s' — dependencies down: %s",
                    c.name, ", ".join(deps_down),
                )
                continue

            attempt_num = state.attempt + 1
            backoff = BACKOFF_SCHEDULE[state.attempt]
            logger.info(
                "Auto-restarting '%s' (attempt %d/%d, next check in %ds)",
                c.name, attempt_num, len(BACKOFF_SCHEDULE), backoff,
            )

            success, message = await restart_container(c.name)
            state.attempt = attempt_num
            state.last_attempt_at = now
            state.next_retry_at = now + timedelta(seconds=backoff)

            await self._write_audit(c.name, "auto", success, message)

    async def _write_audit(
        self, container: str, initiated_by: str, success: bool, details: str,
    ) -> None:
        try:
            await _write_audit_log(
                pool=self._pool,
                user_id=None,
                action="SERVICE_RESTART_SUCCESS" if success else "SERVICE_RESTART_FAILED",
                resource_type="service",
                resource_id=container,
                details={
                    "initiated_by": initiated_by,
                    "username": "system-watchdog",
                    "container": container,
                    "success": success,
                    "message": details,
                },
                ip_address=None,
                hostname="watchdog",
            )
        except Exception:
            logger.exception("Failed to write audit log for %s restart", container)


__all__ = ["ServiceWatchdog"]
