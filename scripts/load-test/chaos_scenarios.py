#!/usr/bin/env python3
"""Extended chaos scenarios for 100-camera scale validation.

Usage:
    python chaos_scenarios.py --prometheus http://localhost:9090 \
        --query-api http://localhost:8000
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path
SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from common import build_query_headers, http_get_json, isoformat_utc, utc_now  # noqa: E402


LOGGER = logging.getLogger("chaos_scenarios")
DEFAULT_OUTPUT_PATH = SCRIPT_DIR.parents[1] / "artifacts" / "load-test" / "100cam-chaos-results.json"


@dataclass(slots=True)
class ScenarioResult:
    """Result of one scale chaos experiment."""

    name: str
    target: str
    status: str
    start_time: str
    end_time: str
    recovery_time_s: float | None
    data_loss: bool | None
    notes: str
    pre_detection_total: int | None = None
    post_detection_total: int | None = None
    pre_max_lag: float | None = None
    post_max_lag: float | None = None
    pre_inference_p95_ms: float | None = None
    post_inference_p95_ms: float | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "target": self.target,
            "status": self.status,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "recovery_time_s": self.recovery_time_s,
            "data_loss": self.data_loss,
            "notes": self.notes,
            "pre_detection_total": self.pre_detection_total,
            "post_detection_total": self.post_detection_total,
            "pre_max_lag": self.pre_max_lag,
            "post_max_lag": self.post_max_lag,
            "pre_inference_p95_ms": self.pre_inference_p95_ms,
            "post_inference_p95_ms": self.post_inference_p95_ms,
        }


class ScaleChaosRunner:
    """Run reversible Docker-native chaos against the scale environment."""

    def __init__(
        self,
        *,
        prometheus_url: str,
        query_api_url: str,
        query_headers: dict[str, str],
    ) -> None:
        self.prometheus_url = prometheus_url.rstrip("/")
        self.query_api_url = query_api_url.rstrip("/")
        self.query_headers = query_headers

    async def kill_kafka_broker(self, container: str, duration_s: int) -> ScenarioResult:
        return await self._run_container_outage(
            name="kill_kafka_broker",
            container=container,
            duration_s=duration_s,
        )

    async def pause_consumer_group(self, container: str, duration_s: int) -> ScenarioResult:
        return await self._run_container_outage(
            name="pause_consumer_group",
            container=container,
            duration_s=duration_s,
        )

    async def restart_timescaledb(self, container: str, duration_s: int) -> ScenarioResult:
        return await self._run_container_outage(
            name="restart_timescaledb",
            container=container,
            duration_s=duration_s,
        )

    async def simulate_edge_wan_outage(
        self,
        *,
        network: str,
        container: str,
        duration_s: int,
    ) -> ScenarioResult:
        start_time = utc_now()
        baseline = await self._capture_baseline()
        disconnected = False
        status = "failed"
        notes = ""

        try:
            await self._run_docker("network", "disconnect", network, container)
            disconnected = True
            await asyncio.sleep(duration_s)
            status = "passed"
            notes = f"disconnected {container} from {network} for {duration_s}s"
        except Exception as exc:
            notes = f"failed to disconnect {container} from {network}: {exc}"
        finally:
            if disconnected:
                await self._safe_docker("network", "connect", network, container)

        recovery_time_s = await self._measure_recovery(baseline)
        current = await self._capture_baseline()
        end_time = utc_now()
        return ScenarioResult(
            name="simulate_edge_wan_outage",
            target=f"{network}:{container}",
            status=status,
            start_time=isoformat_utc(start_time),
            end_time=isoformat_utc(end_time),
            recovery_time_s=recovery_time_s,
            data_loss=_infer_data_loss(baseline["detection_total"], current["detection_total"]),
            notes=notes,
            pre_detection_total=baseline["detection_total"],
            post_detection_total=current["detection_total"],
            pre_max_lag=baseline["max_lag"],
            post_max_lag=current["max_lag"],
            pre_inference_p95_ms=baseline["inference_p95_ms"],
            post_inference_p95_ms=current["inference_p95_ms"],
        )

    async def fail_minio_node(
        self,
        *,
        container: str,
        duration_s: int,
        multi_node_minio: bool,
    ) -> ScenarioResult:
        if not multi_node_minio:
            now = isoformat_utc(utc_now())
            return ScenarioResult(
                name="fail_minio_node",
                target=container,
                status="skipped",
                start_time=now,
                end_time=now,
                recovery_time_s=None,
                data_loss=None,
                notes="skipped because the current deployment is not configured as multi-node MinIO",
            )
        return await self._run_container_outage(
            name="fail_minio_node",
            container=container,
            duration_s=duration_s,
        )

    async def induce_gpu_overload(
        self,
        *,
        overload_command: str | None,
        clear_command: str | None,
        duration_s: int,
    ) -> ScenarioResult:
        start_time = utc_now()
        baseline = await self._capture_baseline()
        if not overload_command:
            now = isoformat_utc(start_time)
            return ScenarioResult(
                name="induce_gpu_overload",
                target="external-overload-command",
                status="skipped",
                start_time=now,
                end_time=now,
                recovery_time_s=None,
                data_loss=None,
                notes="skipped because --overload-command was not provided",
                pre_detection_total=baseline["detection_total"],
                pre_max_lag=baseline["max_lag"],
                pre_inference_p95_ms=baseline["inference_p95_ms"],
            )

        status = "failed"
        notes = ""
        process: asyncio.subprocess.Process | None = None
        try:
            process = await asyncio.create_subprocess_shell(overload_command)
            await asyncio.sleep(duration_s)
            status = "passed"
            notes = f"ran overload command for {duration_s}s"
        except Exception as exc:
            notes = f"failed to run overload command: {exc}"
        finally:
            if process is not None and process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=10.0)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
            if clear_command:
                await self._safe_shell(clear_command)

        recovery_time_s = await self._measure_latency_recovery(baseline)
        current = await self._capture_baseline()
        end_time = utc_now()
        if current["inference_p95_ms"] is not None and baseline["inference_p95_ms"] is not None:
            delta_pct = _relative_delta(
                current["inference_p95_ms"],
                baseline["inference_p95_ms"],
            )
            notes = f"{notes}; inference p95 delta {delta_pct:+.1%}" if notes else f"inference p95 delta {delta_pct:+.1%}"
        return ScenarioResult(
            name="induce_gpu_overload",
            target="external-overload-command",
            status=status,
            start_time=isoformat_utc(start_time),
            end_time=isoformat_utc(end_time),
            recovery_time_s=recovery_time_s,
            data_loss=_infer_data_loss(baseline["detection_total"], current["detection_total"]),
            notes=notes,
            pre_detection_total=baseline["detection_total"],
            post_detection_total=current["detection_total"],
            pre_max_lag=baseline["max_lag"],
            post_max_lag=current["max_lag"],
            pre_inference_p95_ms=baseline["inference_p95_ms"],
            post_inference_p95_ms=current["inference_p95_ms"],
        )

    async def _run_container_outage(
        self,
        *,
        name: str,
        container: str,
        duration_s: int,
    ) -> ScenarioResult:
        start_time = utc_now()
        baseline = await self._capture_baseline()
        status = "failed"
        notes = ""
        was_running = await self._container_running(container)

        try:
            if not was_running:
                raise RuntimeError(f"container {container} is not running")
            await self._run_docker("stop", container)
            await asyncio.sleep(duration_s)
            status = "passed"
            notes = f"stopped {container} for {duration_s}s"
        except Exception as exc:
            notes = f"failed during {name}: {exc}"
        finally:
            if was_running:
                await self._safe_docker("start", container)

        recovery_time_s = await self._measure_recovery(baseline)
        current = await self._capture_baseline()
        end_time = utc_now()
        return ScenarioResult(
            name=name,
            target=container,
            status=status,
            start_time=isoformat_utc(start_time),
            end_time=isoformat_utc(end_time),
            recovery_time_s=recovery_time_s,
            data_loss=_infer_data_loss(baseline["detection_total"], current["detection_total"]),
            notes=notes,
            pre_detection_total=baseline["detection_total"],
            post_detection_total=current["detection_total"],
            pre_max_lag=baseline["max_lag"],
            post_max_lag=current["max_lag"],
            pre_inference_p95_ms=baseline["inference_p95_ms"],
            post_inference_p95_ms=current["inference_p95_ms"],
        )

    async def _capture_baseline(self) -> dict[str, float | int | None]:
        detection_total, max_lag, inference_p95_ms = await asyncio.gather(
            self._query_detection_total(),
            self._query_max_lag(),
            self._query_inference_p95_ms(),
        )
        return {
            "detection_total": detection_total,
            "max_lag": max_lag,
            "inference_p95_ms": inference_p95_ms,
        }

    async def _measure_recovery(
        self,
        baseline: dict[str, float | int | None],
        *,
        timeout_s: int = 300,
    ) -> float | None:
        started_at = time.monotonic()
        baseline_detection = _as_int(baseline.get("detection_total"))
        baseline_lag = _as_float(baseline.get("max_lag"))
        while (time.monotonic() - started_at) < timeout_s:
            current = await self._capture_baseline()
            current_detection = _as_int(current.get("detection_total"))
            current_lag = _as_float(current.get("max_lag"))
            if baseline_detection is not None and current_detection is not None and current_detection > baseline_detection:
                if baseline_lag is None or current_lag is None or current_lag <= (baseline_lag + 100.0):
                    return time.monotonic() - started_at
            await asyncio.sleep(5.0)
        return None

    async def _measure_latency_recovery(
        self,
        baseline: dict[str, float | int | None],
        *,
        timeout_s: int = 300,
    ) -> float | None:
        started_at = time.monotonic()
        baseline_latency = _as_float(baseline.get("inference_p95_ms"))
        while (time.monotonic() - started_at) < timeout_s:
            current_latency = await self._query_inference_p95_ms()
            if baseline_latency is None or current_latency is None:
                return None
            if current_latency <= baseline_latency * 1.15:
                return time.monotonic() - started_at
            await asyncio.sleep(5.0)
        return None

    async def _query_detection_total(self) -> int | None:
        try:
            payload = await asyncio.to_thread(
                http_get_json,
                f"{self.query_api_url}/detections",
                params={"limit": "1"},
                headers=self.query_headers,
            )
        except Exception:
            LOGGER.warning("failed to query detection totals", exc_info=True)
            return None
        total = payload.get("total")
        return int(total) if isinstance(total, int) else None

    async def _query_inference_p95_ms(self) -> float | None:
        return await self._query_scalar(
            "histogram_quantile(0.95, sum by (le) (rate(inference_latency_ms_bucket[5m])))"
        )

    async def _query_max_lag(self) -> float | None:
        lag = await self._query_scalar("max(kafka_consumer_lag)")
        if lag is not None:
            return lag
        lag = await self._query_scalar("max(inference_consumer_lag)")
        if lag is not None:
            return lag
        return await self._query_scalar("max(bulk_consumer_lag)")

    async def _query_scalar(self, expression: str) -> float | None:
        payload = await asyncio.to_thread(
            http_get_json,
            f"{self.prometheus_url}/api/v1/query",
            params={"query": expression},
        )
        if payload.get("status") != "success":
            return None
        result = payload.get("data", {}).get("result", [])
        if not isinstance(result, list) or not result:
            return None
        value = result[0].get("value")
        if not isinstance(value, list) or len(value) != 2:
            return None
        try:
            numeric = float(value[1])
        except (TypeError, ValueError):
            return None
        return numeric if math.isfinite(numeric) else None

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

    async def _safe_docker(self, *args: str) -> None:
        try:
            await self._run_docker(*args)
        except Exception:
            LOGGER.exception("cleanup command failed: docker %s", " ".join(args))

    async def _safe_shell(self, command: str) -> None:
        try:
            process = await asyncio.create_subprocess_shell(command)
            await process.wait()
        except Exception:
            LOGGER.exception("cleanup shell command failed: %s", command)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prometheus", dest="prometheus_url", required=True)
    parser.add_argument("--query-api", dest="query_api_url", default="http://localhost:8000")
    parser.add_argument("--query-jwt-secret", default="pilot-jwt-secret-change-me")
    parser.add_argument("--query-cookie-name", default="access_token")
    parser.add_argument("--query-role", default="admin")
    parser.add_argument("--kafka-container", default="pilot-kafka")
    parser.add_argument("--consumer-container", default="pilot-inference-worker")
    parser.add_argument("--timescaledb-container", default="pilot-timescaledb")
    parser.add_argument("--minio-container", default="pilot-minio")
    parser.add_argument("--multi-node-minio", action="store_true")
    parser.add_argument("--wan-network", default="cilex-pilot")
    parser.add_argument("--wan-container", default="pilot-edge-agent")
    parser.add_argument("--kafka-outage-s", type=int, default=30)
    parser.add_argument("--consumer-pause-s", type=int, default=30)
    parser.add_argument("--wan-outage-s", type=int, default=60)
    parser.add_argument("--timescaledb-restart-s", type=int, default=30)
    parser.add_argument("--minio-outage-s", type=int, default=60)
    parser.add_argument("--gpu-overload-s", type=int, default=120)
    parser.add_argument("--overload-command", default=None)
    parser.add_argument("--overload-clear-command", default=None)
    parser.add_argument("--output-json", type=Path, default=DEFAULT_OUTPUT_PATH)
    parser.add_argument("--log-level", default="INFO")
    return parser.parse_args()


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


async def run_scenarios(args: argparse.Namespace) -> list[ScenarioResult]:
    stop_event = asyncio.Event()
    _install_signal_handlers(stop_event)
    runner = ScaleChaosRunner(
        prometheus_url=args.prometheus_url,
        query_api_url=args.query_api_url,
        query_headers=build_query_headers(
            secret=args.query_jwt_secret,
            cookie_name=args.query_cookie_name,
            role=args.query_role,
            camera_scope=None,
        ),
    )
    results: list[ScenarioResult] = []

    if stop_event.is_set():
        return results
    results.append(await runner.kill_kafka_broker(args.kafka_container, args.kafka_outage_s))
    if stop_event.is_set():
        return results
    results.append(
        await runner.pause_consumer_group(args.consumer_container, args.consumer_pause_s)
    )
    if stop_event.is_set():
        return results
    results.append(
        await runner.simulate_edge_wan_outage(
            network=args.wan_network,
            container=args.wan_container,
            duration_s=args.wan_outage_s,
        )
    )
    if stop_event.is_set():
        return results
    results.append(
        await runner.restart_timescaledb(
            args.timescaledb_container,
            args.timescaledb_restart_s,
        )
    )
    if stop_event.is_set():
        return results
    results.append(
        await runner.fail_minio_node(
            container=args.minio_container,
            duration_s=args.minio_outage_s,
            multi_node_minio=args.multi_node_minio,
        )
    )
    if stop_event.is_set():
        return results
    results.append(
        await runner.induce_gpu_overload(
            overload_command=args.overload_command,
            clear_command=args.overload_clear_command,
            duration_s=args.gpu_overload_s,
        )
    )
    return results


def _write_results(output_path: Path, results: list[ScenarioResult]) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"results": [result.to_dict() for result in results]}
    output_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _install_signal_handlers(stop_event: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()

    def _handle_signal() -> None:
        LOGGER.warning("shutdown signal received")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle_signal)
        except NotImplementedError:
            signal.signal(sig, lambda _signum, _frame: stop_event.set())


def _infer_data_loss(pre: int | None, post: int | None) -> bool | None:
    if pre is None or post is None:
        return None
    return post <= pre


def _as_int(value: object) -> int | None:
    if isinstance(value, int):
        return value
    return None


def _as_float(value: object) -> float | None:
    if isinstance(value, (float, int)):
        return float(value)
    return None


def _relative_delta(current: float, baseline: float) -> float:
    if baseline == 0.0:
        return 0.0
    return (current - baseline) / baseline


def main() -> None:
    args = parse_args()
    setup_logging(args.log_level)
    results = asyncio.run(run_scenarios(args))
    _write_results(args.output_json, results)
    LOGGER.info("wrote %d chaos results to %s", len(results), args.output_json)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # pragma: no cover
        raise SystemExit(str(exc)) from exc
