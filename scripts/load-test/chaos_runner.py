#!/usr/bin/env python3
"""Reversible chaos scenarios for the end-to-end stress-test harness."""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from common import build_query_headers, http_get_json, utc_now  # noqa: E402
from models import ChaosResult, TestConfig  # noqa: E402


LOGGER = logging.getLogger("chaos_runner")


class ChaosRunner:
    """Run reversible chaos scenarios against the Docker-based pilot stack."""

    def __init__(self, config: TestConfig) -> None:
        self.config = config
        self._query_headers = build_query_headers(
            secret=config.query_jwt_secret,
            cookie_name=config.query_cookie_name,
            role=config.query_role,
            camera_scope=None if config.query_role == "admin" else config.camera_ids,
        )

    async def kill_kafka_broker(
        self,
        broker_id: int,
        duration_s: int = 30,
    ) -> ChaosResult:
        """Stop one Kafka broker container, then start it again."""
        container = self._broker_container_name(broker_id)
        pre_row_count = await self._query_detection_total()
        start_time = utc_now()
        was_running = await self._container_running(container)
        success = False
        notes = ""

        try:
            if was_running:
                await self._run_docker("stop", container)
            await asyncio.sleep(duration_s)
            success = True
            notes = f"stopped {container} for {duration_s}s"
        except Exception as exc:
            notes = f"failed to stop {container}: {exc}"
        finally:
            if was_running:
                await self._safe_start(container)

        end_time = utc_now()
        recovery_time_s = await self._measure_recovery(pre_row_count, end_time)
        post_row_count = await self._query_detection_total()
        data_loss = _infer_data_loss(pre_row_count, post_row_count)
        return ChaosResult(
            name="kill_kafka_broker",
            target=container,
            start_time=start_time,
            end_time=end_time,
            recovery_time_s=recovery_time_s,
            data_loss=data_loss,
            success=success,
            notes=notes,
            pre_row_count=pre_row_count,
            post_row_count=post_row_count,
        )

    async def pause_consumer_group(
        self,
        group: str,
        duration_s: int = 30,
    ) -> ChaosResult:
        """Stop a service container mapped to the supplied consumer group."""
        container = self.config.chaos_service_container_map.get(group, group)
        pre_row_count = await self._query_detection_total()
        start_time = utc_now()
        was_running = await self._container_running(container)
        success = False
        notes = ""

        try:
            if was_running:
                await self._run_docker("stop", container)
            await asyncio.sleep(duration_s)
            success = True
            notes = f"paused {group} via {container} for {duration_s}s"
        except Exception as exc:
            notes = f"failed to pause {group}: {exc}"
        finally:
            if was_running:
                await self._safe_start(container)

        end_time = utc_now()
        recovery_time_s = await self._measure_recovery(pre_row_count, end_time)
        post_row_count = await self._query_detection_total()
        data_loss = _infer_data_loss(pre_row_count, post_row_count)
        return ChaosResult(
            name="pause_consumer_group",
            target=f"{group}:{container}",
            start_time=start_time,
            end_time=end_time,
            recovery_time_s=recovery_time_s,
            data_loss=data_loss,
            success=success,
            notes=notes,
            pre_row_count=pre_row_count,
            post_row_count=post_row_count,
        )

    async def simulate_wan_outage(self, duration_s: int = 60) -> ChaosResult:
        """Disconnect the WAN-facing edge container from the pilot Docker network."""
        container = self.config.chaos_wan_target_container
        network = self.config.chaos_network_name
        pre_row_count = await self._query_detection_total()
        start_time = utc_now()
        disconnected = False
        success = False
        notes = ""

        try:
            await self._run_docker("network", "disconnect", network, container)
            disconnected = True
            await asyncio.sleep(duration_s)
            success = True
            notes = f"disconnected {container} from {network} for {duration_s}s"
        except Exception as exc:
            notes = f"failed to simulate WAN outage: {exc}"
        finally:
            if disconnected:
                await self._safe_network_connect(network, container)

        end_time = utc_now()
        recovery_time_s = await self._measure_recovery(pre_row_count, end_time)
        post_row_count = await self._query_detection_total()
        data_loss = _infer_data_loss(pre_row_count, post_row_count)
        return ChaosResult(
            name="simulate_wan_outage",
            target=f"{network}:{container}",
            start_time=start_time,
            end_time=end_time,
            recovery_time_s=recovery_time_s,
            data_loss=data_loss,
            success=success,
            notes=notes,
            pre_row_count=pre_row_count,
            post_row_count=post_row_count,
        )

    async def _measure_recovery(
        self,
        pre_row_count: int | None,
        disruption_end: object,
        *,
        timeout_s: int = 180,
    ) -> float | None:
        if pre_row_count is None:
            return None
        started_at = time.monotonic()
        while (time.monotonic() - started_at) < timeout_s:
            current_count = await self._query_detection_total()
            if current_count is not None and current_count > pre_row_count:
                return time.monotonic() - started_at
            await asyncio.sleep(5)
        LOGGER.warning("recovery probe timed out after disruption ending at %s", disruption_end)
        return None

    async def _query_detection_total(self) -> int | None:
        try:
            payload = await asyncio.to_thread(
                http_get_json,
                f"{self.config.query_api_url.rstrip('/')}/detections",
                params={"limit": "1"},
                headers=self._query_headers,
            )
        except Exception:
            LOGGER.warning("failed to query detection totals for chaos accounting", exc_info=True)
            return None
        total = payload.get("total")
        return int(total) if isinstance(total, int) else None

    async def _container_running(self, container: str) -> bool:
        process = await asyncio.create_subprocess_exec(
            "docker",
            "inspect",
            "-f",
            "{{.State.Running}}",
            container,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _stderr = await process.communicate()
        if process.returncode != 0:
            return False
        return stdout.decode("utf-8").strip().lower() == "true"

    async def _run_docker(self, *args: str) -> None:
        process = await asyncio.create_subprocess_exec(
            "docker",
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
        if process.returncode != 0:
            message = stderr.decode("utf-8").strip() or stdout.decode("utf-8").strip()
            raise RuntimeError(message or f"docker {' '.join(args)} failed")

    async def _safe_start(self, container: str) -> None:
        try:
            await self._run_docker("start", container)
        except Exception:
            LOGGER.exception("failed to restart container %s", container)

    async def _safe_network_connect(self, network: str, container: str) -> None:
        try:
            await self._run_docker("network", "connect", network, container)
        except Exception:
            LOGGER.exception("failed to reconnect %s to %s", container, network)

    def _broker_container_name(self, broker_id: int) -> str:
        template = self.config.chaos_kafka_container_template
        if "{broker_id}" in template:
            return template.format(broker_id=broker_id)
        return template


def _infer_data_loss(
    pre_row_count: int | None,
    post_row_count: int | None,
) -> bool | None:
    if pre_row_count is None or post_row_count is None:
        return None
    return post_row_count <= pre_row_count


if __name__ == "__main__":
    raise SystemExit(
        "chaos_runner.py is a library module. Run run_stress_test.py instead."
    )
