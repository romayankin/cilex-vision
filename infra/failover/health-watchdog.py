#!/usr/bin/env python3
"""Monitor critical services and trigger alerts or failover notifications."""

from __future__ import annotations

import argparse
import importlib
import json
import logging
import os
import socket
import sys
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urlparse
from urllib.request import Request, urlopen

LOGGER = logging.getLogger("health_watchdog")

DEFAULT_INTERVAL_SECONDS = 30.0
DEFAULT_FAILURE_THRESHOLD_SECONDS = 60.0
DEFAULT_DISK_USAGE_THRESHOLD = 90.0
DEFAULT_KAFKA_LAG_THRESHOLD = 10000.0
DEFAULT_PROMETHEUS_URL = "http://localhost:9090"


@dataclass(frozen=True)
class ServiceCheck:
    name: str
    kind: str
    target: str
    timeout_s: float = 5.0


@dataclass
class AlertState:
    first_failed_at: float | None = None
    alerted: bool = False


def require_module(module_name: str, install_hint: str) -> Any:
    try:
        return importlib.import_module(module_name)
    except ImportError as exc:  # pragma: no cover - optional dependency path
        raise RuntimeError(
            f"missing optional dependency '{module_name}'; install {install_hint}"
        ) from exc


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", help="Optional YAML config file.")
    parser.add_argument("--prometheus", dest="prometheus_url")
    parser.add_argument("--interval", type=float)
    parser.add_argument("--alert-webhook")
    parser.add_argument("--failure-threshold", type=float)
    parser.add_argument("--disk-usage-threshold", type=float)
    parser.add_argument("--kafka-lag-threshold", type=float)
    parser.add_argument("--kafka-targets")
    parser.add_argument("--nats-url")
    parser.add_argument("--timescaledb-target")
    parser.add_argument("--minio-url")
    parser.add_argument("--prometheus-health-url")
    parser.add_argument("--triton-url")
    parser.add_argument("--run-once", action="store_true")
    parser.add_argument("--log-level", default="INFO")
    return parser.parse_args()


def load_config_file(path: str) -> dict[str, Any]:
    yaml = require_module("yaml", "pyyaml")
    with open(path, encoding="utf-8") as handle:
        payload = yaml.safe_load(handle) or {}
    if not isinstance(payload, dict):
        raise RuntimeError("watchdog config must be a YAML object")
    return payload


def merged_setting(
    *,
    cli_value: Any,
    config: dict[str, Any],
    key: str,
    env_name: str | None = None,
    default: Any = None,
) -> Any:
    if cli_value is not None:
        return cli_value
    if key in config:
        return config[key]
    if env_name is not None and os.environ.get(env_name) is not None:
        return os.environ[env_name]
    return default


def build_default_checks(config: dict[str, Any], args: argparse.Namespace) -> list[ServiceCheck]:
    kafka_targets = merged_setting(
        cli_value=args.kafka_targets,
        config=config,
        key="kafka_targets",
        env_name="KAFKA_TARGETS",
        default="localhost:19092,localhost:19093,localhost:19094",
    )
    nats_url = merged_setting(
        cli_value=args.nats_url,
        config=config,
        key="nats_url",
        env_name="NATS_HEALTH_URL",
        default="http://localhost:8222/healthz",
    )
    timescaledb_target = merged_setting(
        cli_value=args.timescaledb_target,
        config=config,
        key="timescaledb_target",
        env_name="TIMESCALEDB_TARGET",
        default="localhost:5432",
    )
    minio_url = merged_setting(
        cli_value=args.minio_url,
        config=config,
        key="minio_url",
        env_name="MINIO_HEALTH_URL",
        default="http://localhost:9000/minio/health/live",
    )
    prometheus_health_url = merged_setting(
        cli_value=args.prometheus_health_url,
        config=config,
        key="prometheus_health_url",
        env_name="PROMETHEUS_HEALTH_URL",
        default="http://localhost:9090/-/healthy",
    )
    triton_url = merged_setting(
        cli_value=args.triton_url,
        config=config,
        key="triton_url",
        env_name="TRITON_HEALTH_URL",
        default="http://localhost:8000/v2/health/ready",
    )

    checks: list[ServiceCheck] = []
    for index, target in enumerate(str(kafka_targets).split(","), start=1):
        target = target.strip()
        if target:
            checks.append(ServiceCheck(name=f"kafka-{index}", kind="tcp", target=target))
    checks.extend(
        [
            ServiceCheck(name="nats", kind="http", target=str(nats_url)),
            ServiceCheck(name="timescaledb", kind="tcp", target=str(timescaledb_target)),
            ServiceCheck(name="minio", kind="http", target=str(minio_url)),
            ServiceCheck(name="prometheus", kind="http", target=str(prometheus_health_url)),
            ServiceCheck(name="triton", kind="http", target=str(triton_url)),
        ]
    )
    return checks


def parse_configured_checks(config: dict[str, Any]) -> list[ServiceCheck]:
    raw_checks = config.get("checks")
    if not isinstance(raw_checks, list):
        return []
    checks: list[ServiceCheck] = []
    for entry in raw_checks:
        if not isinstance(entry, dict):
            raise RuntimeError("each config check must be an object")
        name = str(entry.get("name", "")).strip()
        kind = str(entry.get("kind", "")).strip()
        target = str(entry.get("target", "")).strip()
        timeout_s = float(entry.get("timeout_s", 5.0))
        if not name or kind not in {"tcp", "http"} or not target:
            raise RuntimeError("config checks require name, kind (tcp|http), and target")
        checks.append(ServiceCheck(name=name, kind=kind, target=target, timeout_s=timeout_s))
    return checks


def parse_host_port(target: str) -> tuple[str, int]:
    if ":" not in target:
        raise RuntimeError(f"tcp target must be host:port, got {target!r}")
    host, port_raw = target.rsplit(":", 1)
    return host, int(port_raw)


def probe_tcp(target: str, timeout_s: float) -> tuple[bool, str]:
    host, port = parse_host_port(target)
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return True, "tcp ok"
    except OSError as exc:
        return False, str(exc)


def probe_http(target: str, timeout_s: float) -> tuple[bool, str]:
    request = Request(target, method="GET")
    try:
        with urlopen(request, timeout=timeout_s) as response:  # noqa: S310
            status_code = getattr(response, "status", response.getcode())
        if 200 <= status_code < 400:
            return True, f"http {status_code}"
        return False, f"http {status_code}"
    except OSError as exc:
        return False, str(exc)


def instance_label_for_target(target: str, kind: str) -> str | None:
    if kind == "tcp":
        host, port = parse_host_port(target)
        return f"{host}:{port}"
    parsed = urlparse(target)
    if not parsed.hostname:
        return None
    if parsed.port is not None:
        return f"{parsed.hostname}:{parsed.port}"
    if parsed.scheme == "https":
        return f"{parsed.hostname}:443"
    return f"{parsed.hostname}:80"


def prometheus_query(prometheus_url: str, query: str) -> list[dict[str, Any]]:
    request = Request(
        f"{prometheus_url.rstrip('/')}/api/v1/query?{urlencode({'query': query})}",
        method="GET",
    )
    with urlopen(request, timeout=10) as response:  # noqa: S310
        payload = json.loads(response.read().decode("utf-8"))
    if payload.get("status") != "success":
        raise RuntimeError(f"Prometheus query failed: {payload}")
    data = payload.get("data", {})
    result = data.get("result", [])
    if not isinstance(result, list):
        raise RuntimeError("Prometheus query returned a non-list result")
    return result


def prometheus_instance_up(prometheus_url: str, check: ServiceCheck) -> tuple[bool | None, str | None]:
    instance = instance_label_for_target(check.target, check.kind)
    if instance is None:
        return None, None
    try:
        result = prometheus_query(prometheus_url, f'up{{instance="{instance}"}}')
    except OSError as exc:
        return None, f"prometheus up lookup failed: {exc}"
    except RuntimeError as exc:
        return None, str(exc)
    if not result:
        return None, None
    value = float(result[0]["value"][1])
    return value >= 1.0, f"prometheus up={value:.0f}"


def scalar_query(prometheus_url: str, query: str) -> float | None:
    result = prometheus_query(prometheus_url, query)
    if not result:
        return None
    return float(result[0]["value"][1])


def emit_webhook(webhook_url: str | None, payload: dict[str, Any]) -> None:
    if not webhook_url:
        return
    request = Request(
        webhook_url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=10):  # noqa: S310
        return


def emit_alert(kind: str, name: str, message: str, webhook_url: str | None) -> None:
    payload = {"kind": kind, "name": name, "message": message, "timestamp": int(time.time())}
    LOGGER.warning("%s: %s", name, message)
    try:
        emit_webhook(webhook_url, payload)
    except OSError as exc:  # pragma: no cover - external webhook path
        LOGGER.error("failed to deliver alert webhook for %s: %s", name, exc)


def evaluate_service(
    *,
    check: ServiceCheck,
    prometheus_url: str | None,
) -> tuple[bool, str]:
    if check.kind == "http":
        healthy, reason = probe_http(check.target, check.timeout_s)
    else:
        healthy, reason = probe_tcp(check.target, check.timeout_s)
    if not healthy:
        return healthy, reason
    if prometheus_url:
        prom_up, prom_reason = prometheus_instance_up(prometheus_url, check)
        if prom_up is False:
            return False, prom_reason or "prometheus up=0"
        if prom_reason:
            return True, f"{reason}; {prom_reason}"
    return healthy, reason


def run_loop(args: argparse.Namespace) -> int:
    config = load_config_file(args.config) if args.config else {}
    configured_checks = parse_configured_checks(config)
    checks = configured_checks or build_default_checks(config, args)

    prometheus_url = merged_setting(
        cli_value=args.prometheus_url,
        config=config,
        key="prometheus_url",
        env_name="PROMETHEUS_URL",
        default=DEFAULT_PROMETHEUS_URL,
    )
    interval = float(
        merged_setting(
            cli_value=args.interval,
            config=config,
            key="interval",
            env_name="WATCHDOG_INTERVAL",
            default=DEFAULT_INTERVAL_SECONDS,
        )
    )
    failure_threshold = float(
        merged_setting(
            cli_value=args.failure_threshold,
            config=config,
            key="failure_threshold",
            env_name="WATCHDOG_FAILURE_THRESHOLD",
            default=DEFAULT_FAILURE_THRESHOLD_SECONDS,
        )
    )
    disk_usage_threshold = float(
        merged_setting(
            cli_value=args.disk_usage_threshold,
            config=config,
            key="disk_usage_threshold",
            env_name="WATCHDOG_DISK_THRESHOLD",
            default=DEFAULT_DISK_USAGE_THRESHOLD,
        )
    )
    kafka_lag_threshold = float(
        merged_setting(
            cli_value=args.kafka_lag_threshold,
            config=config,
            key="kafka_lag_threshold",
            env_name="WATCHDOG_KAFKA_LAG_THRESHOLD",
            default=DEFAULT_KAFKA_LAG_THRESHOLD,
        )
    )
    alert_webhook = merged_setting(
        cli_value=args.alert_webhook,
        config=config,
        key="alert_webhook",
        env_name="WATCHDOG_ALERT_WEBHOOK",
    )

    service_states = {check.name: AlertState() for check in checks}
    metric_states = {
        "disk_usage": AlertState(),
        "kafka_lag": AlertState(),
    }

    while True:
        now = time.time()
        unhealthy_this_cycle = False

        for check in checks:
            healthy, reason = evaluate_service(check=check, prometheus_url=prometheus_url)
            state = service_states[check.name]
            if healthy:
                LOGGER.info("%s healthy (%s)", check.name, reason)
                if state.alerted:
                    emit_alert("recovery", check.name, f"service recovered: {reason}", alert_webhook)
                state.first_failed_at = None
                state.alerted = False
                continue

            unhealthy_this_cycle = True
            LOGGER.error("%s unhealthy (%s)", check.name, reason)
            if state.first_failed_at is None:
                state.first_failed_at = now
            elapsed = now - state.first_failed_at
            if elapsed >= failure_threshold and not state.alerted:
                emit_alert(
                    "service_down",
                    check.name,
                    f"service unhealthy for {elapsed:.0f}s: {reason}",
                    alert_webhook,
                )
                state.alerted = True

        try:
            disk_usage = scalar_query(
                prometheus_url,
                'max(100 * (node_filesystem_size_bytes{mountpoint="/",fstype!~"tmpfs|overlay"} - node_filesystem_avail_bytes{mountpoint="/",fstype!~"tmpfs|overlay"}) / node_filesystem_size_bytes{mountpoint="/",fstype!~"tmpfs|overlay"})',
            )
            if disk_usage is not None:
                disk_state = metric_states["disk_usage"]
                if disk_usage >= disk_usage_threshold:
                    unhealthy_this_cycle = True
                    if disk_state.first_failed_at is None:
                        disk_state.first_failed_at = now
                    if not disk_state.alerted:
                        emit_alert(
                            "disk_usage_high",
                            "disk_usage",
                            f"disk usage is {disk_usage:.2f}% (threshold {disk_usage_threshold:.2f}%)",
                            alert_webhook,
                        )
                        disk_state.alerted = True
                else:
                    if disk_state.alerted:
                        emit_alert(
                            "recovery",
                            "disk_usage",
                            f"disk usage recovered to {disk_usage:.2f}%",
                            alert_webhook,
                        )
                    disk_state.first_failed_at = None
                    disk_state.alerted = False

            kafka_lag = scalar_query(prometheus_url, "max(kafka_consumer_lag)")
            if kafka_lag is not None:
                lag_state = metric_states["kafka_lag"]
                if kafka_lag >= kafka_lag_threshold:
                    unhealthy_this_cycle = True
                    if lag_state.first_failed_at is None:
                        lag_state.first_failed_at = now
                    if not lag_state.alerted:
                        emit_alert(
                            "kafka_lag_high",
                            "kafka_lag",
                            f"kafka consumer lag is {kafka_lag:.0f} (threshold {kafka_lag_threshold:.0f})",
                            alert_webhook,
                        )
                        lag_state.alerted = True
                else:
                    if lag_state.alerted:
                        emit_alert(
                            "recovery",
                            "kafka_lag",
                            f"kafka consumer lag recovered to {kafka_lag:.0f}",
                            alert_webhook,
                        )
                    lag_state.first_failed_at = None
                    lag_state.alerted = False
        except OSError as exc:
            LOGGER.error("prometheus query failed: %s", exc)
            if args.run_once:
                return 1
        except RuntimeError as exc:
            LOGGER.error("prometheus query failed: %s", exc)
            if args.run_once:
                return 1

        if args.run_once:
            return 1 if unhealthy_this_cycle else 0

        time.sleep(interval)


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        return run_loop(args)
    except KeyboardInterrupt:
        LOGGER.info("health watchdog interrupted")
        return 0
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    sys.exit(main())
