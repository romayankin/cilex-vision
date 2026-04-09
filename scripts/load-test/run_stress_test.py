#!/usr/bin/env python3
"""End-to-end stress test orchestrator.

Usage:
    python run_stress_test.py --duration 3600 --cameras 4 --prometheus http://localhost:9090
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from chaos_runner import ChaosRunner  # noqa: E402
from generate_stress_report import generate_report  # noqa: E402
from load_generator import LoadGenerator  # noqa: E402
from metric_collector import MetricCollector  # noqa: E402
from models import ChaosResult, TestConfig  # noqa: E402


LOGGER = logging.getLogger("run_stress_test")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration", dest="duration_s", type=int, default=3600)
    parser.add_argument("--cameras", dest="camera_count", type=int, default=4)
    parser.add_argument("--fps", dest="camera_fps", type=int, default=5)
    parser.add_argument("--qps", dest="query_qps", type=int, default=10)
    parser.add_argument("--prometheus", dest="prometheus_url", required=True)
    parser.add_argument("--query-api", dest="query_api_url", default="http://localhost:8080")
    parser.add_argument("--kafka-bootstrap", default="localhost:19092")
    parser.add_argument("--kafka-security-protocol", default="PLAINTEXT")
    parser.add_argument("--minio-url", default="localhost:9000")
    parser.add_argument("--minio-access-key", default="minioadmin")
    parser.add_argument("--minio-secret-key", default="minioadmin123")
    parser.add_argument("--minio-secure", action="store_true")
    parser.add_argument("--source-bucket", default="frame-blobs")
    parser.add_argument("--metrics-interval-s", type=float, default=15.0)
    parser.add_argument("--camera-prefix", default="stress-cam")
    parser.add_argument("--site-id", default="pilot-site")
    parser.add_argument("--report-path", type=Path, default=None)
    parser.add_argument("--replay-frame-dir", type=Path, default=None)
    parser.add_argument("--query-jwt-secret", default="pilot-jwt-secret-change-me")
    parser.add_argument("--query-cookie-name", default="access_token")
    parser.add_argument("--query-role", default="admin")
    parser.add_argument("--chaos-enabled", action="store_true")
    parser.add_argument("--chaos-kafka-container-template", default="pilot-kafka")
    parser.add_argument("--chaos-network-name", default="cilex-pilot")
    parser.add_argument("--chaos-wan-target-container", default="pilot-edge-agent")
    parser.add_argument("--log-level", default="INFO")
    return parser.parse_args()


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


async def run_stress_test(config: TestConfig) -> list[ChaosResult]:
    """Run the configured stress test and write the Markdown report."""
    stop_event = asyncio.Event()
    _install_signal_handlers(stop_event)

    load_generator = LoadGenerator(config)
    metric_collector = MetricCollector(config.prometheus_url, camera_count=config.camera_count)
    chaos_runner = ChaosRunner(config)
    camera_stop_events = {camera_id: asyncio.Event() for camera_id in config.camera_ids}
    camera_tasks: dict[str, asyncio.Task[None]] = {}
    query_stop_event = asyncio.Event()
    query_task: asyncio.Task[None] | None = None
    metric_stop_event = asyncio.Event()
    metric_task: asyncio.Task[None] | None = None
    chaos_task: asyncio.Task[list[ChaosResult]] | None = None
    chaos_results: list[ChaosResult] = []

    ramp_up_s, sustained_s, ramp_down_s = _phase_windows(config.duration_s)
    LOGGER.info(
        "phase windows: ramp_up=%ss sustained=%ss ramp_down=%ss",
        round(ramp_up_s, 1),
        round(sustained_s, 1),
        round(ramp_down_s, 1),
    )

    try:
        await load_generator.start()
        metric_task = asyncio.create_task(
            _metric_collection_loop(
                metric_collector,
                metric_stop_event,
                config.metrics_interval_s,
            )
        )

        await _ramp_up_phase(
            load_generator=load_generator,
            config=config,
            stop_event=stop_event,
            camera_stop_events=camera_stop_events,
            camera_tasks=camera_tasks,
            duration_s=ramp_up_s,
        )

        if not stop_event.is_set():
            LOGGER.info("starting sustained query load at %d QPS", config.query_qps)
            query_task = asyncio.create_task(
                load_generator.generate_query_load(
                    config.query_qps,
                    stop_event=query_stop_event,
                )
            )
            if config.chaos_enabled:
                chaos_task = asyncio.create_task(
                    _run_chaos_schedule(
                        chaos_runner=chaos_runner,
                        stop_event=stop_event,
                        sustained_window_s=sustained_s,
                    )
                )
            await _sleep_or_stop(stop_event, sustained_s)

        query_stop_event.set()
        if query_task is not None:
            await asyncio.gather(query_task, return_exceptions=True)

        await _ramp_down_phase(
            stop_event=stop_event,
            camera_stop_events=camera_stop_events,
            camera_tasks=camera_tasks,
            duration_s=ramp_down_s,
        )
        if chaos_task is not None:
            chaos_results = await chaos_task
    finally:
        stop_event.set()
        query_stop_event.set()
        metric_stop_event.set()
        for event in camera_stop_events.values():
            event.set()
        if chaos_task is not None and not chaos_task.done():
            chaos_task.cancel()
            await asyncio.gather(chaos_task, return_exceptions=True)
        if metric_task is not None:
            await asyncio.gather(metric_task, return_exceptions=True)
        if camera_tasks:
            await asyncio.gather(*camera_tasks.values(), return_exceptions=True)
        await load_generator.close()

    generate_report(metric_collector.history, chaos_results, config, config.report_path)
    LOGGER.info("report written to %s", config.report_path)
    return chaos_results


async def _metric_collection_loop(
    collector: MetricCollector,
    stop_event: asyncio.Event,
    interval_s: float,
) -> None:
    while not stop_event.is_set():
        try:
            snapshot = await collector.collect_snapshot()
            LOGGER.info("snapshot collected at %s", snapshot.collected_at.isoformat())
        except Exception:
            LOGGER.exception("failed to collect Prometheus snapshot")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_s)
        except asyncio.TimeoutError:
            continue


async def _ramp_up_phase(
    *,
    load_generator: LoadGenerator,
    config: TestConfig,
    stop_event: asyncio.Event,
    camera_stop_events: dict[str, asyncio.Event],
    camera_tasks: dict[str, asyncio.Task[None]],
    duration_s: float,
) -> None:
    if not config.camera_ids:
        return
    delay_s = duration_s / max(len(config.camera_ids), 1)
    for camera_id in config.camera_ids:
        if stop_event.is_set():
            return
        LOGGER.info("starting camera load for %s", camera_id)
        camera_tasks[camera_id] = asyncio.create_task(
            load_generator.generate_camera_load(
                camera_id,
                fps=config.camera_fps,
                stop_event=camera_stop_events[camera_id],
            )
        )
        await _sleep_or_stop(stop_event, delay_s)


async def _ramp_down_phase(
    *,
    stop_event: asyncio.Event,
    camera_stop_events: dict[str, asyncio.Event],
    camera_tasks: dict[str, asyncio.Task[None]],
    duration_s: float,
) -> None:
    if not camera_stop_events:
        return
    delay_s = duration_s / max(len(camera_stop_events), 1)
    for camera_id in reversed(list(camera_stop_events)):
        LOGGER.info("stopping camera load for %s", camera_id)
        camera_stop_events[camera_id].set()
        await _sleep_or_stop(stop_event, delay_s)
    if camera_tasks:
        await asyncio.gather(*camera_tasks.values(), return_exceptions=True)


async def _run_chaos_schedule(
    *,
    chaos_runner: ChaosRunner,
    stop_event: asyncio.Event,
    sustained_window_s: float,
) -> list[ChaosResult]:
    if sustained_window_s <= 0:
        return []

    schedule = [
        (min(max(sustained_window_s * 0.20, 5.0), sustained_window_s), "kill_kafka_broker"),
        (min(max(sustained_window_s * 0.50, 10.0), sustained_window_s), "pause_consumer_group"),
        (min(max(sustained_window_s * 0.80, 15.0), sustained_window_s), "simulate_wan_outage"),
    ]
    results: list[ChaosResult] = []
    schedule_started = asyncio.get_running_loop().time()

    for offset_s, action in schedule:
        await _sleep_until(stop_event, schedule_started + offset_s)
        if stop_event.is_set():
            break
        if action == "kill_kafka_broker":
            LOGGER.info("running chaos scenario: kill_kafka_broker")
            results.append(await chaos_runner.kill_kafka_broker(0, duration_s=30))
        elif action == "pause_consumer_group":
            LOGGER.info("running chaos scenario: pause_consumer_group")
            results.append(
                await chaos_runner.pause_consumer_group("detector-worker", duration_s=30)
            )
        elif action == "simulate_wan_outage":
            LOGGER.info("running chaos scenario: simulate_wan_outage")
            results.append(await chaos_runner.simulate_wan_outage(duration_s=60))
    return results


def _phase_windows(duration_s: int) -> tuple[float, float, float]:
    ramp_up_s = min(300.0, max(duration_s / 12.0, 30.0))
    ramp_down_s = ramp_up_s
    sustained_s = max(float(duration_s) - ramp_up_s - ramp_down_s, 0.0)
    return ramp_up_s, sustained_s, ramp_down_s


async def _sleep_or_stop(stop_event: asyncio.Event, duration_s: float) -> None:
    if duration_s <= 0:
        return
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=duration_s)
    except asyncio.TimeoutError:
        return


async def _sleep_until(stop_event: asyncio.Event, target_monotonic: float) -> None:
    now = asyncio.get_running_loop().time()
    delay_s = max(target_monotonic - now, 0.0)
    await _sleep_or_stop(stop_event, delay_s)


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


def _build_config(args: argparse.Namespace) -> TestConfig:
    report_path = args.report_path
    if report_path is None:
        report_path = SCRIPT_DIR.parents[1] / "docs" / "evaluation-results" / "stress-test-report.md"
    return TestConfig(
        duration_s=args.duration_s,
        camera_count=args.camera_count,
        camera_fps=args.camera_fps,
        query_qps=args.query_qps,
        prometheus_url=args.prometheus_url,
        query_api_url=args.query_api_url,
        chaos_enabled=args.chaos_enabled,
        kafka_bootstrap=args.kafka_bootstrap,
        kafka_security_protocol=args.kafka_security_protocol,
        minio_url=args.minio_url,
        minio_access_key=args.minio_access_key,
        minio_secret_key=args.minio_secret_key,
        minio_secure=args.minio_secure,
        source_bucket=args.source_bucket,
        metrics_interval_s=args.metrics_interval_s,
        camera_prefix=args.camera_prefix,
        site_id=args.site_id,
        report_path=report_path,
        replay_frame_dir=args.replay_frame_dir,
        query_jwt_secret=args.query_jwt_secret,
        query_cookie_name=args.query_cookie_name,
        query_role=args.query_role,
        chaos_kafka_container_template=args.chaos_kafka_container_template,
        chaos_network_name=args.chaos_network_name,
        chaos_wan_target_container=args.chaos_wan_target_container,
    )


def main() -> None:
    args = parse_args()
    setup_logging(args.log_level)
    config = _build_config(args)
    asyncio.run(run_stress_test(config))


if __name__ == "__main__":
    main()
