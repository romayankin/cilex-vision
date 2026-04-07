#!/usr/bin/env python3
"""Parametric monthly cost model for Cilex Vision."""

from __future__ import annotations

import argparse
import importlib
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PARAMS_PATH = Path(__file__).with_name("params.yaml")
DEFAULT_TOPICS_PATH = REPO_ROOT / "infra" / "kafka" / "topics.yaml"
DEFAULT_COMPOSE_PATH = REPO_ROOT / "infra" / "docker-compose.yml"
DEFAULT_XLSX_PATH = REPO_ROOT / "artifacts" / "cost-model" / "cost-model.xlsx"
ASSUMPTION_MARKER = "REPLACE WITH MEASURED"
SECONDS_PER_DAY = 86_400.0
KB_PER_GB = 1_000_000.0
GB_PER_TB = 1_000.0


@dataclass(frozen=True)
class AssumedNumber:
    value: float
    note: str


@dataclass(frozen=True)
class ScenarioDefinition:
    name: str
    duty_cycle: float
    note: str


@dataclass(frozen=True)
class KafkaTopicSpec:
    name: str
    partitions: int
    replication_factor: int
    cleanup_policy: str
    retention_ms: int

    @property
    def retention_days(self) -> float | None:
        if self.retention_ms < 0:
            return None
        return self.retention_ms / 1000.0 / 60.0 / 60.0 / 24.0


@dataclass(frozen=True)
class ComposeInventory:
    service_names: tuple[str, ...]
    group_counts: dict[str, int]


@dataclass(frozen=True)
class CostModelInputs:
    params_path: Path
    camera_counts: tuple[int, ...]
    scenarios: tuple[ScenarioDefinition, ...]
    bitrate_mbps: float
    inference_fps: float
    detections_per_frame: float
    active_tracks_per_camera: float
    avg_event_clip_seconds: float
    events_per_active_track_hour: float
    clip_transcode_ratio: float
    attribute_messages_per_detection: float
    embedding_updates_per_active_track_hour: float
    gpu_cost_monthly_usd: float
    cameras_per_gpu: float
    gpu_headroom_factor: float
    central_frame_blob_retention_days: float
    warm_event_clips_retention_days: float
    timeseries_metadata_retention_days: float
    timeseries_compress_after_days: float
    hot_object_usd_per_gb_month: float
    warm_object_usd_per_gb_month: float
    kafka_broker_disk_usd_per_gb_month: float
    timescaledb_nvme_usd_per_gb_month: float
    detection_row_kb_uncompressed: float
    track_observation_row_kb_uncompressed: float
    hypertable_compression_ratio: float
    db_index_overhead_factor: float
    kafka_broker_storage_overhead_factor: float
    kafka_avg_message_kb: dict[str, float]
    service_unit_costs_monthly_usd: dict[str, float]
    assumption_rows: tuple[tuple[str, str, str], ...]


@dataclass(frozen=True)
class TopicStorageRow:
    topic: str
    cleanup_policy: str
    partitions: int
    retention_days: float | None
    message_volume_per_day: float
    steady_state_gb: float


@dataclass(frozen=True)
class SummaryRow:
    cameras: int
    duty_cycle: float
    raw_source_gb_day: float
    central_load_gb_day: float
    central_inference_fps: float
    gpu_nodes_needed: int
    event_clips_day: float
    hot_object_storage_gb: float
    warm_object_storage_gb: float
    kafka_storage_gb: float
    timescaledb_storage_gb: float
    fixed_infrastructure_usd: float
    gpu_usd: float
    hot_object_usd: float
    warm_object_usd: float
    kafka_storage_usd: float
    timescaledb_storage_usd: float
    total_monthly_usd: float
    topic_storage_rows: tuple[TopicStorageRow, ...]


@dataclass(frozen=True)
class ScenarioReport:
    scenario: ScenarioDefinition
    summary_rows: tuple[SummaryRow, ...]


def require_module(module_name: str, install_hint: str) -> Any:
    try:
        return importlib.import_module(module_name)
    except ImportError as exc:  # pragma: no cover - environment-specific
        raise RuntimeError(f"missing optional dependency '{module_name}'; install {install_hint}") from exc


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--params",
        type=Path,
        default=DEFAULT_PARAMS_PATH,
        help="YAML file with calibration and cost-model inputs.",
    )
    parser.add_argument(
        "--topics-file",
        type=Path,
        default=DEFAULT_TOPICS_PATH,
        help="Kafka topic catalog used for partition and retention assumptions.",
    )
    parser.add_argument(
        "--compose-file",
        type=Path,
        default=DEFAULT_COMPOSE_PATH,
        help="Docker Compose file used to map fixed infrastructure components.",
    )
    parser.add_argument(
        "--xlsx-output",
        type=Path,
        default=DEFAULT_XLSX_PATH,
        help="Excel workbook path written with openpyxl.",
    )
    parser.add_argument(
        "--skip-xlsx",
        action="store_true",
        help="Skip workbook generation. Useful when openpyxl is not installed.",
    )
    return parser.parse_args()


def load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"YAML file not found: {path}")
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise ValueError(f"expected a YAML mapping at {path}")
    return payload


def parse_assumed_number(raw: Any, *, path: str) -> AssumedNumber:
    if not isinstance(raw, dict):
        raise ValueError(f"{path} must be a mapping with value/note")
    if "value" not in raw or "note" not in raw:
        raise ValueError(f"{path} must contain value and note")
    note = str(raw["note"])
    if ASSUMPTION_MARKER not in note:
        raise ValueError(f"{path} note must include '{ASSUMPTION_MARKER}'")
    try:
        value = float(raw["value"])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{path}.value must be numeric") from exc
    return AssumedNumber(value=value, note=note)


def parse_camera_counts(raw: Any) -> tuple[tuple[int, ...], tuple[str, str, str]]:
    if not isinstance(raw, dict):
        raise ValueError("cost_model.camera_counts must be a mapping")
    note = str(raw.get("note") or "")
    if ASSUMPTION_MARKER not in note:
        raise ValueError("cost_model.camera_counts.note must include 'REPLACE WITH MEASURED'")
    values = raw.get("values")
    if not isinstance(values, list) or not values:
        raise ValueError("cost_model.camera_counts.values must be a non-empty list")
    counts = tuple(int(value) for value in values)
    if any(value <= 0 for value in counts):
        raise ValueError("cost_model.camera_counts.values must all be > 0")
    return counts, ("cost_model.camera_counts", ", ".join(str(item) for item in counts), note)


def parse_scenarios(raw: Any) -> tuple[tuple[ScenarioDefinition, ...], tuple[tuple[str, str, str], ...]]:
    if not isinstance(raw, dict) or not raw:
        raise ValueError("cost_model.motion_duty_cycle_scenarios must be a non-empty mapping")
    scenarios: list[ScenarioDefinition] = []
    assumption_rows: list[tuple[str, str, str]] = []
    for name, item in raw.items():
        parsed = parse_assumed_number(item, path=f"cost_model.motion_duty_cycle_scenarios.{name}")
        if not 0.0 < parsed.value <= 1.0:
            raise ValueError(f"scenario duty cycle must be in (0, 1]: {name}")
        scenarios.append(ScenarioDefinition(name=str(name), duty_cycle=parsed.value, note=parsed.note))
        assumption_rows.append((f"cost_model.motion_duty_cycle_scenarios.{name}", f"{parsed.value}", parsed.note))
    ordered = sorted(
        scenarios,
        key=lambda item: (0 if item.name == "P25" else 1 if item.name == "P50" else 2 if item.name == "P90" else 3, item.name),
    )
    return tuple(ordered), tuple(assumption_rows)


def parse_number_group(raw: Any, *, prefix: str) -> tuple[dict[str, float], tuple[tuple[str, str, str], ...]]:
    if not isinstance(raw, dict) or not raw:
        raise ValueError(f"{prefix} must be a non-empty mapping")
    values: dict[str, float] = {}
    assumption_rows: list[tuple[str, str, str]] = []
    for key, item in raw.items():
        parsed = parse_assumed_number(item, path=f"{prefix}.{key}")
        values[str(key)] = parsed.value
        assumption_rows.append((f"{prefix}.{key}", f"{parsed.value}", parsed.note))
    return values, tuple(assumption_rows)


def load_cost_model_inputs(path: Path) -> CostModelInputs:
    payload = load_yaml(path)
    cost_model = payload.get("cost_model")
    if not isinstance(cost_model, dict):
        raise ValueError("params.yaml must contain a cost_model mapping")
    marker = str(cost_model.get("assumption_marker") or "")
    if marker != ASSUMPTION_MARKER:
        raise ValueError("cost_model.assumption_marker must equal 'REPLACE WITH MEASURED'")

    assumption_rows: list[tuple[str, str, str]] = []
    camera_counts, camera_count_row = parse_camera_counts(cost_model.get("camera_counts"))
    assumption_rows.append(camera_count_row)
    scenarios, scenario_rows = parse_scenarios(cost_model.get("motion_duty_cycle_scenarios"))
    assumption_rows.extend(scenario_rows)

    workload_values, rows = parse_number_group(cost_model.get("workload"), prefix="cost_model.workload")
    assumption_rows.extend(rows)
    gpu_values, rows = parse_number_group(cost_model.get("gpu"), prefix="cost_model.gpu")
    assumption_rows.extend(rows)
    retention_values, rows = parse_number_group(cost_model.get("retention_days"), prefix="cost_model.retention_days")
    assumption_rows.extend(rows)
    storage_costs, rows = parse_number_group(
        cost_model.get("storage_costs_usd_per_gb_month"),
        prefix="cost_model.storage_costs_usd_per_gb_month",
    )
    assumption_rows.extend(rows)
    database_values, rows = parse_number_group(cost_model.get("database"), prefix="cost_model.database")
    assumption_rows.extend(rows)

    kafka = cost_model.get("kafka")
    if not isinstance(kafka, dict):
        raise ValueError("cost_model.kafka must be a mapping")
    kafka_broker_storage_overhead_factor = parse_assumed_number(
        kafka.get("broker_storage_overhead_factor"),
        path="cost_model.kafka.broker_storage_overhead_factor",
    )
    assumption_rows.append(
        (
            "cost_model.kafka.broker_storage_overhead_factor",
            f"{kafka_broker_storage_overhead_factor.value}",
            kafka_broker_storage_overhead_factor.note,
        )
    )
    kafka_avg_message_kb, rows = parse_number_group(
        kafka.get("avg_message_kb"),
        prefix="cost_model.kafka.avg_message_kb",
    )
    assumption_rows.extend(rows)

    service_unit_costs, rows = parse_number_group(
        cost_model.get("service_unit_costs_monthly_usd"),
        prefix="cost_model.service_unit_costs_monthly_usd",
    )
    assumption_rows.extend(rows)

    return CostModelInputs(
        params_path=path,
        camera_counts=camera_counts,
        scenarios=scenarios,
        bitrate_mbps=workload_values["bitrate_mbps"],
        inference_fps=workload_values["inference_fps"],
        detections_per_frame=workload_values["detections_per_frame"],
        active_tracks_per_camera=workload_values["active_tracks_per_camera"],
        avg_event_clip_seconds=workload_values["avg_event_clip_seconds"],
        events_per_active_track_hour=workload_values["events_per_active_track_hour"],
        clip_transcode_ratio=workload_values["clip_transcode_ratio"],
        attribute_messages_per_detection=workload_values["attribute_messages_per_detection"],
        embedding_updates_per_active_track_hour=workload_values["embedding_updates_per_active_track_hour"],
        gpu_cost_monthly_usd=gpu_values["gpu_cost_monthly_usd"],
        cameras_per_gpu=gpu_values["cameras_per_gpu"],
        gpu_headroom_factor=gpu_values["gpu_headroom_factor"],
        central_frame_blob_retention_days=retention_values["central_frame_blobs"],
        warm_event_clips_retention_days=retention_values["warm_event_clips"],
        timeseries_metadata_retention_days=retention_values["timeseries_metadata"],
        timeseries_compress_after_days=retention_values["timeseries_compress_after"],
        hot_object_usd_per_gb_month=storage_costs["hot_object"],
        warm_object_usd_per_gb_month=storage_costs["warm_object"],
        kafka_broker_disk_usd_per_gb_month=storage_costs["kafka_broker_disk"],
        timescaledb_nvme_usd_per_gb_month=storage_costs["timescaledb_nvme"],
        detection_row_kb_uncompressed=database_values["detection_row_kb_uncompressed"],
        track_observation_row_kb_uncompressed=database_values["track_observation_row_kb_uncompressed"],
        hypertable_compression_ratio=database_values["hypertable_compression_ratio"],
        db_index_overhead_factor=database_values["index_overhead_factor"],
        kafka_broker_storage_overhead_factor=kafka_broker_storage_overhead_factor.value,
        kafka_avg_message_kb=kafka_avg_message_kb,
        service_unit_costs_monthly_usd=service_unit_costs,
        assumption_rows=tuple(assumption_rows),
    )


def load_topic_catalog(path: Path) -> tuple[KafkaTopicSpec, ...]:
    payload = load_yaml(path)
    defaults = payload.get("defaults")
    if not isinstance(defaults, dict):
        raise ValueError("topics.yaml missing defaults mapping")
    default_replication_factor = int(defaults.get("replication_factor", 1))
    raw_topics = payload.get("topics")
    if not isinstance(raw_topics, list) or not raw_topics:
        raise ValueError("topics.yaml missing topics list")

    topics: list[KafkaTopicSpec] = []
    for raw_topic in raw_topics:
        if not isinstance(raw_topic, dict):
            raise ValueError("each topic entry must be a mapping")
        topics.append(
            KafkaTopicSpec(
                name=str(raw_topic["name"]),
                partitions=int(raw_topic["partitions"]),
                replication_factor=int(raw_topic.get("replication_factor", default_replication_factor)),
                cleanup_policy=str(raw_topic.get("cleanup_policy", "delete")),
                retention_ms=int(raw_topic.get("retention_ms", -1)),
            )
        )
    return tuple(topics)


def load_compose_inventory(path: Path) -> ComposeInventory:
    payload = load_yaml(path)
    services = payload.get("services")
    if not isinstance(services, dict) or not services:
        raise ValueError("docker-compose.yml missing services mapping")
    service_names = tuple(sorted(str(name) for name in services))
    group_counts = {
        "kafka_broker": sum(1 for name in service_names if re.fullmatch(r"kafka-\d+", name)),
        "kafka_ui": 1 if "kafka-ui" in services else 0,
        "nats": 1 if "nats" in services else 0,
        "timescaledb": 1 if "timescaledb" in services else 0,
        "minio": 1 if "minio" in services else 0,
        "redis": 1 if "redis" in services else 0,
        "prometheus": 1 if "prometheus" in services else 0,
        "grafana": 1 if "grafana" in services else 0,
        "mlflow": 1 if "mlflow" in services else 0,
    }
    return ComposeInventory(service_names=service_names, group_counts=group_counts)


def gb_per_day_from_mbps(mbps: float) -> float:
    return mbps * 1_000_000.0 / 8.0 * SECONDS_PER_DAY / 1_000_000_000.0


def gb_from_kb(kilobytes: float) -> float:
    return kilobytes / KB_PER_GB


def tb_from_gb(gigabytes: float) -> float:
    return gigabytes / GB_PER_TB


def format_float(value: float, *, decimals: int = 2) -> str:
    return f"{value:,.{decimals}f}"


def format_table(headers: list[str], rows: list[list[str]]) -> str:
    widths = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))

    def build_row(values: list[str]) -> str:
        return "| " + " | ".join(value.ljust(widths[index]) for index, value in enumerate(values)) + " |"

    separator = "| " + " | ".join("-" * width for width in widths) + " |"
    lines = [build_row(headers), separator]
    lines.extend(build_row(row) for row in rows)
    return "\n".join(lines)


def message_volume_for_topic(
    *,
    topic_name: str,
    central_frames_per_day: float,
    tracklet_messages_per_day: float,
    attribute_messages_per_day: float,
    event_clips_per_day: float,
    active_embedding_keys: float,
) -> float:
    if topic_name == "frames.sampled.refs":
        return central_frames_per_day
    if topic_name == "tracklets.local":
        return tracklet_messages_per_day
    if topic_name == "attributes.jobs":
        return attribute_messages_per_day
    if topic_name == "mtmc.active_embeddings":
        return active_embedding_keys
    if topic_name == "events.raw":
        return event_clips_per_day
    if topic_name in {"archive.transcode.requested", "archive.transcode.completed"}:
        return event_clips_per_day
    raise ValueError(f"no traffic model is defined for Kafka topic {topic_name}")


def compute_fixed_infrastructure_cost(
    inputs: CostModelInputs,
    inventory: ComposeInventory,
) -> float:
    total = 0.0
    for group_name, unit_cost in inputs.service_unit_costs_monthly_usd.items():
        count = inventory.group_counts.get(group_name)
        if count is None:
            raise ValueError(f"compose inventory does not define service group {group_name}")
        total += unit_cost * count
    return total


def compute_summary_row(
    *,
    cameras: int,
    scenario: ScenarioDefinition,
    inputs: CostModelInputs,
    topics: tuple[KafkaTopicSpec, ...],
    fixed_infrastructure_usd: float,
) -> SummaryRow:
    raw_source_gb_day = cameras * gb_per_day_from_mbps(inputs.bitrate_mbps)
    central_load_gb_day = raw_source_gb_day * scenario.duty_cycle
    central_inference_fps = cameras * inputs.inference_fps * scenario.duty_cycle
    active_camera_equivalents = cameras * scenario.duty_cycle * inputs.gpu_headroom_factor
    gpu_nodes_needed = 0 if cameras == 0 else max(1, math.ceil(active_camera_equivalents / inputs.cameras_per_gpu))
    event_clips_day = (
        cameras
        * inputs.active_tracks_per_camera
        * scenario.duty_cycle
        * inputs.events_per_active_track_hour
        * 24.0
    )

    hot_object_storage_gb = central_load_gb_day * inputs.central_frame_blob_retention_days
    warm_clip_gb_day = (
        event_clips_day
        * inputs.avg_event_clip_seconds
        * inputs.bitrate_mbps
        * inputs.clip_transcode_ratio
        / 8_000.0
    )
    warm_object_storage_gb = warm_clip_gb_day * inputs.warm_event_clips_retention_days

    central_frames_per_day = central_inference_fps * SECONDS_PER_DAY
    detections_per_day = central_frames_per_day * inputs.detections_per_frame
    tracklet_messages_per_day = central_frames_per_day * inputs.active_tracks_per_camera
    attribute_messages_per_day = detections_per_day * inputs.attribute_messages_per_detection
    active_embedding_keys = cameras * inputs.active_tracks_per_camera * scenario.duty_cycle

    topic_storage_rows: list[TopicStorageRow] = []
    kafka_storage_gb = 0.0
    for topic in topics:
        if topic.name not in inputs.kafka_avg_message_kb:
            raise ValueError(
                f"cost_model.kafka.avg_message_kb is missing an entry for Kafka topic {topic.name}"
            )
        message_volume = message_volume_for_topic(
            topic_name=topic.name,
            central_frames_per_day=central_frames_per_day,
            tracklet_messages_per_day=tracklet_messages_per_day,
            attribute_messages_per_day=attribute_messages_per_day,
            event_clips_per_day=event_clips_day,
            active_embedding_keys=active_embedding_keys,
        )
        if topic.cleanup_policy == "compact":
            steady_state_gb = (
                gb_from_kb(message_volume * inputs.kafka_avg_message_kb[topic.name])
                * topic.replication_factor
                * inputs.kafka_broker_storage_overhead_factor
            )
        else:
            retention_days = topic.retention_days or 0.0
            steady_state_gb = (
                gb_from_kb(message_volume * inputs.kafka_avg_message_kb[topic.name])
                * retention_days
                * topic.replication_factor
                * inputs.kafka_broker_storage_overhead_factor
            )
        kafka_storage_gb += steady_state_gb
        topic_storage_rows.append(
            TopicStorageRow(
                topic=topic.name,
                cleanup_policy=topic.cleanup_policy,
                partitions=topic.partitions,
                retention_days=topic.retention_days,
                message_volume_per_day=message_volume,
                steady_state_gb=steady_state_gb,
            )
        )

    daily_timeseries_gb = (
        gb_from_kb(detections_per_day * inputs.detection_row_kb_uncompressed)
        + gb_from_kb(tracklet_messages_per_day * inputs.track_observation_row_kb_uncompressed)
    )
    hot_days = min(inputs.timeseries_metadata_retention_days, inputs.timeseries_compress_after_days)
    cold_days = max(inputs.timeseries_metadata_retention_days - hot_days, 0.0)
    timescaledb_storage_gb = (
        daily_timeseries_gb * hot_days
        + daily_timeseries_gb * cold_days / inputs.hypertable_compression_ratio
    ) * inputs.db_index_overhead_factor

    gpu_usd = gpu_nodes_needed * inputs.gpu_cost_monthly_usd
    hot_object_usd = hot_object_storage_gb * inputs.hot_object_usd_per_gb_month
    warm_object_usd = warm_object_storage_gb * inputs.warm_object_usd_per_gb_month
    kafka_storage_usd = kafka_storage_gb * inputs.kafka_broker_disk_usd_per_gb_month
    timescaledb_storage_usd = timescaledb_storage_gb * inputs.timescaledb_nvme_usd_per_gb_month
    total_monthly_usd = (
        fixed_infrastructure_usd
        + gpu_usd
        + hot_object_usd
        + warm_object_usd
        + kafka_storage_usd
        + timescaledb_storage_usd
    )

    return SummaryRow(
        cameras=cameras,
        duty_cycle=scenario.duty_cycle,
        raw_source_gb_day=raw_source_gb_day,
        central_load_gb_day=central_load_gb_day,
        central_inference_fps=central_inference_fps,
        gpu_nodes_needed=gpu_nodes_needed,
        event_clips_day=event_clips_day,
        hot_object_storage_gb=hot_object_storage_gb,
        warm_object_storage_gb=warm_object_storage_gb,
        kafka_storage_gb=kafka_storage_gb,
        timescaledb_storage_gb=timescaledb_storage_gb,
        fixed_infrastructure_usd=fixed_infrastructure_usd,
        gpu_usd=gpu_usd,
        hot_object_usd=hot_object_usd,
        warm_object_usd=warm_object_usd,
        kafka_storage_usd=kafka_storage_usd,
        timescaledb_storage_usd=timescaledb_storage_usd,
        total_monthly_usd=total_monthly_usd,
        topic_storage_rows=tuple(topic_storage_rows),
    )


def build_reports(
    *,
    inputs: CostModelInputs,
    topics: tuple[KafkaTopicSpec, ...],
    inventory: ComposeInventory,
) -> tuple[ScenarioReport, ...]:
    fixed_infrastructure_usd = compute_fixed_infrastructure_cost(inputs, inventory)
    reports: list[ScenarioReport] = []
    for scenario in inputs.scenarios:
        summary_rows = tuple(
            compute_summary_row(
                cameras=camera_count,
                scenario=scenario,
                inputs=inputs,
                topics=topics,
                fixed_infrastructure_usd=fixed_infrastructure_usd,
            )
            for camera_count in inputs.camera_counts
        )
        reports.append(ScenarioReport(scenario=scenario, summary_rows=summary_rows))
    return tuple(reports)


def build_summary_table(report: ScenarioReport) -> str:
    headers = [
        "Cameras",
        "Duty",
        "Raw/day GB",
        "Central/day GB",
        "Central FPS",
        "GPU Nodes",
        "Events/day",
        "Hot TB",
        "Warm TB",
        "Kafka GB",
        "Timescale GB",
        "Monthly USD",
    ]
    rows = [
        [
            str(row.cameras),
            format_float(row.duty_cycle * 100.0, decimals=1) + "%",
            format_float(row.raw_source_gb_day),
            format_float(row.central_load_gb_day),
            format_float(row.central_inference_fps),
            str(row.gpu_nodes_needed),
            format_float(row.event_clips_day),
            format_float(tb_from_gb(row.hot_object_storage_gb), decimals=3),
            format_float(tb_from_gb(row.warm_object_storage_gb), decimals=3),
            format_float(row.kafka_storage_gb),
            format_float(row.timescaledb_storage_gb),
            format_float(row.total_monthly_usd),
        ]
        for row in report.summary_rows
    ]
    return format_table(headers, rows)


def build_cost_breakdown_table(report: ScenarioReport) -> str:
    headers = [
        "Cameras",
        "Fixed Infra USD",
        "GPU USD",
        "Hot Object USD",
        "Warm Object USD",
        "Kafka USD",
        "Timescale USD",
        "Total USD",
    ]
    rows = [
        [
            str(row.cameras),
            format_float(row.fixed_infrastructure_usd),
            format_float(row.gpu_usd),
            format_float(row.hot_object_usd),
            format_float(row.warm_object_usd),
            format_float(row.kafka_storage_usd),
            format_float(row.timescaledb_storage_usd),
            format_float(row.total_monthly_usd),
        ]
        for row in report.summary_rows
    ]
    return format_table(headers, rows)


def render_stdout_report(
    *,
    reports: tuple[ScenarioReport, ...],
    inputs: CostModelInputs,
    topics: tuple[KafkaTopicSpec, ...],
    inventory: ComposeInventory,
    topics_path: Path,
    compose_path: Path,
    xlsx_output: Path | None,
) -> str:
    topic_count = len(topics)
    total_partitions = sum(topic.partitions for topic in topics)
    kafka_brokers = inventory.group_counts["kafka_broker"]
    lines = [
        "Cilex Vision Parametric Cost Model",
        "",
        f"Inputs: {inputs.params_path}",
        f"Kafka catalog: {topic_count} topics / {total_partitions} partitions / {kafka_brokers} brokers from {topics_path}",
        f"Infra components from {compose_path}: {', '.join(inventory.service_names)}",
    ]
    if xlsx_output is not None:
        lines.append(f"Excel workbook: {xlsx_output}")
    lines.append("")
    for report in reports:
        lines.append(
            f"Scenario {report.scenario.name} "
            f"(motion duty cycle {report.scenario.duty_cycle * 100.0:.1f}%)"
        )
        lines.append(build_summary_table(report))
        lines.append("")
        lines.append(build_cost_breakdown_table(report))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def autosize_openpyxl_columns(sheet: Any) -> None:
    for column_cells in sheet.columns:
        letter = column_cells[0].column_letter
        width = max(len(str(cell.value or "")) for cell in column_cells) + 2
        sheet.column_dimensions[letter].width = min(width, 48)


def write_excel_workbook(
    *,
    path: Path,
    reports: tuple[ScenarioReport, ...],
    inputs: CostModelInputs,
    topics: tuple[KafkaTopicSpec, ...],
    inventory: ComposeInventory,
) -> None:
    openpyxl = require_module("openpyxl", "openpyxl")
    styles = require_module("openpyxl.styles", "openpyxl")
    workbook = openpyxl.Workbook()
    default_sheet = workbook.active
    workbook.remove(default_sheet)

    header_font = styles.Font(bold=True)

    assumptions = workbook.create_sheet("assumptions")
    assumptions.append(["Parameter", "Value", "Note"])
    for cell in assumptions[1]:
        cell.font = header_font
    for row in inputs.assumption_rows:
        assumptions.append(list(row))
    assumptions.append([])
    assumptions.append(["Topic count", len(topics), "Derived from infra/kafka/topics.yaml"])
    assumptions.append(["Total partitions", sum(topic.partitions for topic in topics), "Derived from infra/kafka/topics.yaml"])
    assumptions.append(["Compose services", ", ".join(inventory.service_names), "Derived from infra/docker-compose.yml"])
    autosize_openpyxl_columns(assumptions)
    assumptions.freeze_panes = "A2"

    for report in reports:
        sheet = workbook.create_sheet(report.scenario.name.lower())
        sheet.append([f"Scenario {report.scenario.name}", report.scenario.duty_cycle, report.scenario.note])
        sheet.append([])
        summary_headers = [
            "Cameras",
            "Duty",
            "Raw/day GB",
            "Central/day GB",
            "Central FPS",
            "GPU Nodes",
            "Events/day",
            "Hot Object GB",
            "Warm Object GB",
            "Kafka GB",
            "Timescale GB",
            "Monthly USD",
        ]
        sheet.append(summary_headers)
        for cell in sheet[sheet.max_row]:
            cell.font = header_font
        for row in report.summary_rows:
            sheet.append(
                [
                    row.cameras,
                    row.duty_cycle,
                    row.raw_source_gb_day,
                    row.central_load_gb_day,
                    row.central_inference_fps,
                    row.gpu_nodes_needed,
                    row.event_clips_day,
                    row.hot_object_storage_gb,
                    row.warm_object_storage_gb,
                    row.kafka_storage_gb,
                    row.timescaledb_storage_gb,
                    row.total_monthly_usd,
                ]
            )
        sheet.append([])
        cost_headers = [
            "Cameras",
            "Fixed Infra USD",
            "GPU USD",
            "Hot Object USD",
            "Warm Object USD",
            "Kafka USD",
            "Timescale USD",
            "Total USD",
        ]
        sheet.append(cost_headers)
        for cell in sheet[sheet.max_row]:
            cell.font = header_font
        for row in report.summary_rows:
            sheet.append(
                [
                    row.cameras,
                    row.fixed_infrastructure_usd,
                    row.gpu_usd,
                    row.hot_object_usd,
                    row.warm_object_usd,
                    row.kafka_storage_usd,
                    row.timescaledb_storage_usd,
                    row.total_monthly_usd,
                ]
            )
        sheet.append([])
        topic_headers = [
            "Cameras",
            "Topic",
            "Cleanup",
            "Partitions",
            "Retention Days",
            "Messages/Day",
            "Steady-State GB",
        ]
        sheet.append(topic_headers)
        for cell in sheet[sheet.max_row]:
            cell.font = header_font
        for summary_row in report.summary_rows:
            for topic_row in summary_row.topic_storage_rows:
                sheet.append(
                    [
                        summary_row.cameras,
                        topic_row.topic,
                        topic_row.cleanup_policy,
                        topic_row.partitions,
                        topic_row.retention_days,
                        topic_row.message_volume_per_day,
                        topic_row.steady_state_gb,
                    ]
                )
        sheet.freeze_panes = "A3"
        autosize_openpyxl_columns(sheet)

    path.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(path)


def main() -> None:
    args = parse_args()
    inputs = load_cost_model_inputs(args.params)
    topics = load_topic_catalog(args.topics_file)
    inventory = load_compose_inventory(args.compose_file)
    reports = build_reports(inputs=inputs, topics=topics, inventory=inventory)

    xlsx_output: Path | None = None
    if not args.skip_xlsx:
        xlsx_output = args.xlsx_output
        write_excel_workbook(
            path=xlsx_output,
            reports=reports,
            inputs=inputs,
            topics=topics,
            inventory=inventory,
        )

    print(
        render_stdout_report(
            reports=reports,
            inputs=inputs,
            topics=topics,
            inventory=inventory,
            topics_path=args.topics_file,
            compose_path=args.compose_file,
            xlsx_output=xlsx_output,
        ),
        end="",
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # pragma: no cover - CLI boundary
        raise SystemExit(str(exc)) from exc
