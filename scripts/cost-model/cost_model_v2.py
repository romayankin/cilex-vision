#!/usr/bin/env python3
"""Parametric monthly cost model v2 for Cilex Vision — measured pilot parameters.

Extends the v1 cost model with:
- Measured parameters replacing pilot estimates (source provenance)
- MTMC infrastructure costs (Re-ID GPU, FAISS memory, checkpoint storage)
- Tiered storage costs (hot / warm / cold)
- Annotation pipeline costs (CVAT, hard-example mining, annotator time)
- Shadow deployment overhead (GPU + Kafka during model rollout)
- Estimated vs measured comparison output
"""

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
DEFAULT_PARAMS_PATH = Path(__file__).with_name("params-measured.yaml")
DEFAULT_TOPICS_PATH = REPO_ROOT / "infra" / "kafka" / "topics.yaml"
DEFAULT_COMPOSE_PATH = REPO_ROOT / "infra" / "docker-compose.yml"
DEFAULT_XLSX_PATH = REPO_ROOT / "artifacts" / "cost-model" / "cost-model-v2.xlsx"
SECONDS_PER_DAY = 86_400.0
KB_PER_GB = 1_000_000.0
GB_PER_TB = 1_000.0


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MeasuredNumber:
    """A numeric parameter with provenance and comparison to original estimate."""

    value: float
    source: str
    original_estimate: float

    @property
    def delta_pct(self) -> float:
        if self.original_estimate == 0.0:
            return 0.0
        return (self.value - self.original_estimate) / self.original_estimate * 100.0


@dataclass(frozen=True)
class ScenarioDefinition:
    name: str
    duty_cycle: float
    source: str
    original_estimate: float


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
    # workload
    bitrate_mbps: float
    inference_fps: float
    detections_per_frame: float
    active_tracks_per_camera: float
    avg_event_clip_seconds: float
    events_per_active_track_hour: float
    clip_transcode_ratio: float
    attribute_messages_per_detection: float
    embedding_updates_per_active_track_hour: float
    # gpu
    gpu_cost_monthly_usd: float
    cameras_per_gpu: float
    gpu_headroom_factor: float
    # retention
    central_frame_blob_retention_days: float
    warm_event_clips_retention_days: float
    timeseries_metadata_retention_days: float
    timeseries_compress_after_days: float
    # storage costs
    hot_object_usd_per_gb_month: float
    warm_object_usd_per_gb_month: float
    cold_object_usd_per_gb_month: float
    kafka_broker_disk_usd_per_gb_month: float
    timescaledb_nvme_usd_per_gb_month: float
    # database
    detection_row_kb_uncompressed: float
    track_observation_row_kb_uncompressed: float
    hypertable_compression_ratio: float
    db_index_overhead_factor: float
    # kafka
    kafka_broker_storage_overhead_factor: float
    kafka_avg_message_kb: dict[str, float]
    # fixed infra
    service_unit_costs_monthly_usd: dict[str, float]
    # mtmc
    faiss_memory_mb_per_10k_embeddings: float
    mtmc_checkpoint_storage_mb: float
    reid_gpu_fraction: float
    # annotation
    cvat_hosting_monthly_usd: float
    mining_compute_monthly_usd: float
    annotator_cost_per_hour_usd: float
    daily_mining_hours: float
    # shadow
    shadow_gpu_overhead_factor: float
    shadow_kafka_overhead_factor: float
    # comparison data
    comparison_rows: tuple[tuple[str, str, str, str, str], ...]


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
    # storage volumes
    hot_object_storage_gb: float
    warm_object_storage_gb: float
    cold_object_storage_gb: float
    kafka_storage_gb: float
    timescaledb_storage_gb: float
    # costs
    fixed_infrastructure_usd: float
    gpu_usd: float
    hot_object_usd: float
    warm_object_usd: float
    cold_object_usd: float
    kafka_storage_usd: float
    timescaledb_storage_usd: float
    mtmc_infra_usd: float
    annotation_usd: float
    shadow_overhead_usd: float
    total_monthly_usd: float
    topic_storage_rows: tuple[TopicStorageRow, ...]


@dataclass(frozen=True)
class ScenarioReport:
    scenario: ScenarioDefinition
    summary_rows: tuple[SummaryRow, ...]


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------


def require_module(module_name: str, install_hint: str) -> Any:
    try:
        return importlib.import_module(module_name)
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            f"missing optional dependency '{module_name}'; install {install_hint}"
        ) from exc


def gb_per_day_from_mbps(mbps: float) -> float:
    return mbps * 1_000_000.0 / 8.0 * SECONDS_PER_DAY / 1_000_000_000.0


def gb_from_kb(kilobytes: float) -> float:
    return kilobytes / KB_PER_GB


def tb_from_gb(gigabytes: float) -> float:
    return gigabytes / GB_PER_TB


def format_float(value: float, *, decimals: int = 2) -> str:
    return f"{value:,.{decimals}f}"


def format_table(headers: list[str], rows: list[list[str]]) -> str:
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    def build_row(values: list[str]) -> str:
        return "| " + " | ".join(v.ljust(widths[i]) for i, v in enumerate(values)) + " |"

    separator = "| " + " | ".join("-" * w for w in widths) + " |"
    lines = [build_row(headers), separator]
    lines.extend(build_row(r) for r in rows)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# YAML loading — measured parameter format
# ---------------------------------------------------------------------------


def load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"YAML file not found: {path}")
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise ValueError(f"expected a YAML mapping at {path}")
    return payload


def parse_measured_number(raw: Any, *, path: str) -> MeasuredNumber:
    """Parse a measured parameter entry: {value, source, [original_estimate]}."""
    if not isinstance(raw, dict):
        raise ValueError(f"{path} must be a mapping with value/source")
    if "value" not in raw or "source" not in raw:
        raise ValueError(f"{path} must contain value and source")
    try:
        value = float(raw["value"])
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{path}.value must be numeric") from exc
    source = str(raw["source"])
    original = float(raw.get("original_estimate", value))
    return MeasuredNumber(value=value, source=source, original_estimate=original)


def parse_camera_counts(raw: Any) -> tuple[int, ...]:
    if not isinstance(raw, dict):
        raise ValueError("cost_model.camera_counts must be a mapping")
    values = raw.get("values")
    if not isinstance(values, list) or not values:
        raise ValueError("cost_model.camera_counts.values must be a non-empty list")
    counts = tuple(int(v) for v in values)
    if any(v <= 0 for v in counts):
        raise ValueError("cost_model.camera_counts.values must all be > 0")
    return counts


def parse_scenarios(
    raw: Any,
) -> tuple[tuple[ScenarioDefinition, ...], list[tuple[str, str, str, str, str]]]:
    if not isinstance(raw, dict) or not raw:
        raise ValueError("cost_model.motion_duty_cycle_scenarios must be a non-empty mapping")
    scenarios: list[ScenarioDefinition] = []
    comparison_rows: list[tuple[str, str, str, str, str]] = []
    for name, item in raw.items():
        m = parse_measured_number(item, path=f"motion_duty_cycle_scenarios.{name}")
        if not 0.0 < m.value <= 1.0:
            raise ValueError(f"scenario duty cycle must be in (0, 1]: {name}")
        scenarios.append(
            ScenarioDefinition(
                name=str(name),
                duty_cycle=m.value,
                source=m.source,
                original_estimate=m.original_estimate,
            )
        )
        comparison_rows.append((
            f"duty_cycle.{name}",
            f"{m.original_estimate}",
            f"{m.value}",
            f"{m.delta_pct:+.1f}%",
            m.source,
        ))
    ordered = sorted(
        scenarios,
        key=lambda s: (
            0 if s.name == "P25" else 1 if s.name == "P50" else 2 if s.name == "P90" else 3,
            s.name,
        ),
    )
    return tuple(ordered), comparison_rows


def parse_measured_group(
    raw: Any, *, prefix: str,
) -> tuple[dict[str, float], list[tuple[str, str, str, str, str]]]:
    """Parse a group of measured numbers, returning values and comparison rows."""
    if not isinstance(raw, dict) or not raw:
        raise ValueError(f"{prefix} must be a non-empty mapping")
    values: dict[str, float] = {}
    comparison_rows: list[tuple[str, str, str, str, str]] = []
    for key, item in raw.items():
        m = parse_measured_number(item, path=f"{prefix}.{key}")
        values[str(key)] = m.value
        comparison_rows.append((
            f"{prefix}.{key}",
            f"{m.original_estimate}",
            f"{m.value}",
            f"{m.delta_pct:+.1f}%",
            m.source,
        ))
    return values, comparison_rows


def parse_simple_section(raw: Any, *, prefix: str) -> dict[str, float]:
    """Parse a section of simple key-value pairs (new cost categories)."""
    if not isinstance(raw, dict) or not raw:
        raise ValueError(f"{prefix} must be a non-empty mapping")
    return {str(k): float(v) for k, v in raw.items()}


def load_cost_model_inputs(path: Path) -> CostModelInputs:
    payload = load_yaml(path)
    cost_model = payload.get("cost_model")
    if not isinstance(cost_model, dict):
        raise ValueError("params YAML must contain a cost_model mapping")

    comparison_rows: list[tuple[str, str, str, str, str]] = []

    camera_counts = parse_camera_counts(cost_model.get("camera_counts"))
    scenarios, scenario_cmp = parse_scenarios(cost_model.get("motion_duty_cycle_scenarios"))
    comparison_rows.extend(scenario_cmp)

    workload, cmp = parse_measured_group(cost_model.get("workload"), prefix="workload")
    comparison_rows.extend(cmp)
    gpu_values, cmp = parse_measured_group(cost_model.get("gpu"), prefix="gpu")
    comparison_rows.extend(cmp)
    retention, cmp = parse_measured_group(cost_model.get("retention_days"), prefix="retention_days")
    comparison_rows.extend(cmp)
    storage_costs, cmp = parse_measured_group(
        cost_model.get("storage_costs_usd_per_gb_month"),
        prefix="storage_costs",
    )
    comparison_rows.extend(cmp)
    db_values, cmp = parse_measured_group(cost_model.get("database"), prefix="database")
    comparison_rows.extend(cmp)

    kafka = cost_model.get("kafka")
    if not isinstance(kafka, dict):
        raise ValueError("cost_model.kafka must be a mapping")
    broker_overhead = parse_measured_number(
        kafka.get("broker_storage_overhead_factor"),
        path="kafka.broker_storage_overhead_factor",
    )
    comparison_rows.append((
        "kafka.broker_overhead",
        f"{broker_overhead.original_estimate}",
        f"{broker_overhead.value}",
        f"{broker_overhead.delta_pct:+.1f}%",
        broker_overhead.source,
    ))
    kafka_msg_kb, cmp = parse_measured_group(
        kafka.get("avg_message_kb"), prefix="kafka.avg_message_kb",
    )
    comparison_rows.extend(cmp)

    service_costs, cmp = parse_measured_group(
        cost_model.get("service_unit_costs_monthly_usd"),
        prefix="service_costs",
    )
    comparison_rows.extend(cmp)

    # New cost categories — simple key-value sections
    mtmc = parse_simple_section(cost_model.get("mtmc") or {}, prefix="mtmc")
    annotation = parse_simple_section(cost_model.get("annotation") or {}, prefix="annotation")
    shadow = parse_simple_section(cost_model.get("shadow") or {}, prefix="shadow")

    return CostModelInputs(
        params_path=path,
        camera_counts=camera_counts,
        scenarios=scenarios,
        bitrate_mbps=workload["bitrate_mbps"],
        inference_fps=workload["inference_fps"],
        detections_per_frame=workload["detections_per_frame"],
        active_tracks_per_camera=workload["active_tracks_per_camera"],
        avg_event_clip_seconds=workload["avg_event_clip_seconds"],
        events_per_active_track_hour=workload["events_per_active_track_hour"],
        clip_transcode_ratio=workload["clip_transcode_ratio"],
        attribute_messages_per_detection=workload["attribute_messages_per_detection"],
        embedding_updates_per_active_track_hour=workload["embedding_updates_per_active_track_hour"],
        gpu_cost_monthly_usd=gpu_values["gpu_cost_monthly_usd"],
        cameras_per_gpu=gpu_values["cameras_per_gpu"],
        gpu_headroom_factor=gpu_values["gpu_headroom_factor"],
        central_frame_blob_retention_days=retention["central_frame_blobs"],
        warm_event_clips_retention_days=retention["warm_event_clips"],
        timeseries_metadata_retention_days=retention["timeseries_metadata"],
        timeseries_compress_after_days=retention["timeseries_compress_after"],
        hot_object_usd_per_gb_month=storage_costs["hot_object"],
        warm_object_usd_per_gb_month=storage_costs["warm_object"],
        cold_object_usd_per_gb_month=storage_costs.get("cold_object", storage_costs["warm_object"]),
        kafka_broker_disk_usd_per_gb_month=storage_costs["kafka_broker_disk"],
        timescaledb_nvme_usd_per_gb_month=storage_costs["timescaledb_nvme"],
        detection_row_kb_uncompressed=db_values["detection_row_kb_uncompressed"],
        track_observation_row_kb_uncompressed=db_values["track_observation_row_kb_uncompressed"],
        hypertable_compression_ratio=db_values["hypertable_compression_ratio"],
        db_index_overhead_factor=db_values["index_overhead_factor"],
        kafka_broker_storage_overhead_factor=broker_overhead.value,
        kafka_avg_message_kb=kafka_msg_kb,
        service_unit_costs_monthly_usd=service_costs,
        faiss_memory_mb_per_10k_embeddings=mtmc.get("faiss_memory_mb_per_10k_embeddings", 50.0),
        mtmc_checkpoint_storage_mb=mtmc.get("checkpoint_storage_mb", 100.0),
        reid_gpu_fraction=mtmc.get("reid_gpu_fraction", 0.1),
        cvat_hosting_monthly_usd=annotation.get("cvat_hosting_monthly_usd", 50.0),
        mining_compute_monthly_usd=annotation.get("mining_compute_monthly_usd", 25.0),
        annotator_cost_per_hour_usd=annotation.get("annotator_cost_per_hour_usd", 25.0),
        daily_mining_hours=annotation.get("daily_mining_hours", 0.5),
        shadow_gpu_overhead_factor=shadow.get("shadow_gpu_overhead_factor", 0.15),
        shadow_kafka_overhead_factor=shadow.get("shadow_kafka_overhead_factor", 0.1),
        comparison_rows=tuple(comparison_rows),
    )


# ---------------------------------------------------------------------------
# Topic catalog and compose inventory (same as v1)
# ---------------------------------------------------------------------------


def load_topic_catalog(path: Path) -> tuple[KafkaTopicSpec, ...]:
    payload = load_yaml(path)
    defaults = payload.get("defaults")
    if not isinstance(defaults, dict):
        raise ValueError("topics.yaml missing defaults mapping")
    default_rf = int(defaults.get("replication_factor", 1))
    raw_topics = payload.get("topics")
    if not isinstance(raw_topics, list) or not raw_topics:
        raise ValueError("topics.yaml missing topics list")
    topics: list[KafkaTopicSpec] = []
    for entry in raw_topics:
        if not isinstance(entry, dict):
            raise ValueError("each topic entry must be a mapping")
        topics.append(KafkaTopicSpec(
            name=str(entry["name"]),
            partitions=int(entry["partitions"]),
            replication_factor=int(entry.get("replication_factor", default_rf)),
            cleanup_policy=str(entry.get("cleanup_policy", "delete")),
            retention_ms=int(entry.get("retention_ms", -1)),
        ))
    return tuple(topics)


def load_compose_inventory(path: Path) -> ComposeInventory:
    payload = load_yaml(path)
    services = payload.get("services")
    if not isinstance(services, dict) or not services:
        raise ValueError("docker-compose.yml missing services mapping")
    service_names = tuple(sorted(str(n) for n in services))
    group_counts = {
        "kafka_broker": sum(1 for n in service_names if re.fullmatch(r"kafka-\d+", n)),
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


# ---------------------------------------------------------------------------
# Cost computation
# ---------------------------------------------------------------------------


def message_volume_for_topic(
    *,
    topic_name: str,
    central_frames_per_day: float,
    detections_per_day: float,
    tracklet_messages_per_day: float,
    attribute_messages_per_day: float,
    event_clips_per_day: float,
    active_embedding_keys: float,
) -> float:
    if topic_name in ("frames.sampled.refs", "frames.decoded.refs"):
        return central_frames_per_day
    if topic_name == "tracklets.local":
        return tracklet_messages_per_day
    if topic_name == "bulk.detections":
        return detections_per_day
    if topic_name == "attributes.jobs":
        return attribute_messages_per_day
    if topic_name == "mtmc.active_embeddings":
        return active_embedding_keys
    if topic_name == "events.raw":
        return event_clips_per_day
    if topic_name in ("archive.transcode.requested", "archive.transcode.completed"):
        return event_clips_per_day
    raise ValueError(f"no traffic model for Kafka topic {topic_name}")


def compute_fixed_infrastructure_cost(
    inputs: CostModelInputs,
    inventory: ComposeInventory,
) -> float:
    total = 0.0
    for group_name, unit_cost in inputs.service_unit_costs_monthly_usd.items():
        count = inventory.group_counts.get(group_name)
        if count is None:
            raise ValueError(f"compose inventory missing service group {group_name}")
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
    # Data volumes
    raw_source_gb_day = cameras * gb_per_day_from_mbps(inputs.bitrate_mbps)
    central_load_gb_day = raw_source_gb_day * scenario.duty_cycle
    central_inference_fps = cameras * inputs.inference_fps * scenario.duty_cycle
    active_camera_equivalents = cameras * scenario.duty_cycle * inputs.gpu_headroom_factor
    gpu_nodes_needed = (
        0 if cameras == 0
        else max(1, math.ceil(active_camera_equivalents / inputs.cameras_per_gpu))
    )
    event_clips_day = (
        cameras
        * inputs.active_tracks_per_camera
        * scenario.duty_cycle
        * inputs.events_per_active_track_hour
        * 24.0
    )

    # Hot storage: frame blobs (retention from lifecycle policy)
    hot_object_storage_gb = central_load_gb_day * inputs.central_frame_blob_retention_days

    # Warm storage: event clips
    warm_clip_gb_day = (
        event_clips_day
        * inputs.avg_event_clip_seconds
        * inputs.bitrate_mbps
        * inputs.clip_transcode_ratio
        / 8_000.0
    )
    warm_object_storage_gb = warm_clip_gb_day * inputs.warm_event_clips_retention_days

    # Cold storage: debug traces (~2% sample) + MTMC checkpoints
    central_frames_per_day = central_inference_fps * SECONDS_PER_DAY
    debug_trace_gb_day = central_frames_per_day * 0.02 * 10.0 / KB_PER_GB
    cold_object_storage_gb = debug_trace_gb_day * 30.0 + inputs.mtmc_checkpoint_storage_mb / 1000.0

    # Kafka topic storage
    detections_per_day = central_frames_per_day * inputs.detections_per_frame
    tracklet_messages_per_day = central_frames_per_day * inputs.active_tracks_per_camera
    attribute_messages_per_day = detections_per_day * inputs.attribute_messages_per_detection
    active_embedding_keys = cameras * inputs.active_tracks_per_camera * scenario.duty_cycle

    topic_storage_rows: list[TopicStorageRow] = []
    kafka_storage_gb = 0.0
    for topic in topics:
        if topic.name not in inputs.kafka_avg_message_kb:
            raise ValueError(
                f"cost_model.kafka.avg_message_kb is missing an entry for topic {topic.name}"
            )
        msg_volume = message_volume_for_topic(
            topic_name=topic.name,
            central_frames_per_day=central_frames_per_day,
            detections_per_day=detections_per_day,
            tracklet_messages_per_day=tracklet_messages_per_day,
            attribute_messages_per_day=attribute_messages_per_day,
            event_clips_per_day=event_clips_day,
            active_embedding_keys=active_embedding_keys,
        )
        if topic.cleanup_policy == "compact":
            steady_gb = (
                gb_from_kb(msg_volume * inputs.kafka_avg_message_kb[topic.name])
                * topic.replication_factor
                * inputs.kafka_broker_storage_overhead_factor
            )
        else:
            ret_days = topic.retention_days or 0.0
            steady_gb = (
                gb_from_kb(msg_volume * inputs.kafka_avg_message_kb[topic.name])
                * ret_days
                * topic.replication_factor
                * inputs.kafka_broker_storage_overhead_factor
            )
        kafka_storage_gb += steady_gb
        topic_storage_rows.append(TopicStorageRow(
            topic=topic.name,
            cleanup_policy=topic.cleanup_policy,
            partitions=topic.partitions,
            retention_days=topic.retention_days,
            message_volume_per_day=msg_volume,
            steady_state_gb=steady_gb,
        ))

    # TimescaleDB storage
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

    # Cost calculations
    gpu_usd = gpu_nodes_needed * inputs.gpu_cost_monthly_usd
    hot_object_usd = hot_object_storage_gb * inputs.hot_object_usd_per_gb_month
    warm_object_usd = warm_object_storage_gb * inputs.warm_object_usd_per_gb_month
    cold_object_usd = cold_object_storage_gb * inputs.cold_object_usd_per_gb_month
    kafka_storage_usd = kafka_storage_gb * inputs.kafka_broker_disk_usd_per_gb_month
    timescaledb_storage_usd = timescaledb_storage_gb * inputs.timescaledb_nvme_usd_per_gb_month

    # MTMC infrastructure: Re-ID GPU share
    mtmc_infra_usd = inputs.reid_gpu_fraction * gpu_usd

    # Annotation pipeline: flat monthly cost
    annotation_usd = (
        inputs.cvat_hosting_monthly_usd
        + inputs.mining_compute_monthly_usd
        + inputs.annotator_cost_per_hour_usd * inputs.daily_mining_hours * 30.0
    )

    # Shadow deployment overhead (amortized: assume ~1 month per quarter = 33%)
    shadow_amortization = 1.0 / 3.0
    shadow_overhead_usd = (
        inputs.shadow_gpu_overhead_factor * gpu_usd
        + inputs.shadow_kafka_overhead_factor * kafka_storage_usd
    ) * shadow_amortization

    total_monthly_usd = (
        fixed_infrastructure_usd
        + gpu_usd
        + hot_object_usd
        + warm_object_usd
        + cold_object_usd
        + kafka_storage_usd
        + timescaledb_storage_usd
        + mtmc_infra_usd
        + annotation_usd
        + shadow_overhead_usd
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
        cold_object_storage_gb=cold_object_storage_gb,
        kafka_storage_gb=kafka_storage_gb,
        timescaledb_storage_gb=timescaledb_storage_gb,
        fixed_infrastructure_usd=fixed_infrastructure_usd,
        gpu_usd=gpu_usd,
        hot_object_usd=hot_object_usd,
        warm_object_usd=warm_object_usd,
        cold_object_usd=cold_object_usd,
        kafka_storage_usd=kafka_storage_usd,
        timescaledb_storage_usd=timescaledb_storage_usd,
        mtmc_infra_usd=mtmc_infra_usd,
        annotation_usd=annotation_usd,
        shadow_overhead_usd=shadow_overhead_usd,
        total_monthly_usd=total_monthly_usd,
        topic_storage_rows=tuple(topic_storage_rows),
    )


def build_reports(
    *,
    inputs: CostModelInputs,
    topics: tuple[KafkaTopicSpec, ...],
    inventory: ComposeInventory,
) -> tuple[ScenarioReport, ...]:
    fixed_usd = compute_fixed_infrastructure_cost(inputs, inventory)
    reports: list[ScenarioReport] = []
    for scenario in inputs.scenarios:
        rows = tuple(
            compute_summary_row(
                cameras=count,
                scenario=scenario,
                inputs=inputs,
                topics=topics,
                fixed_infrastructure_usd=fixed_usd,
            )
            for count in inputs.camera_counts
        )
        reports.append(ScenarioReport(scenario=scenario, summary_rows=rows))
    return tuple(reports)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


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
        "Cold GB",
        "Kafka GB",
        "Timescale GB",
        "Monthly USD",
    ]
    rows = [
        [
            str(r.cameras),
            format_float(r.duty_cycle * 100.0, decimals=1) + "%",
            format_float(r.raw_source_gb_day),
            format_float(r.central_load_gb_day),
            format_float(r.central_inference_fps),
            str(r.gpu_nodes_needed),
            format_float(r.event_clips_day),
            format_float(tb_from_gb(r.hot_object_storage_gb), decimals=3),
            format_float(tb_from_gb(r.warm_object_storage_gb), decimals=3),
            format_float(r.cold_object_storage_gb),
            format_float(r.kafka_storage_gb),
            format_float(r.timescaledb_storage_gb),
            format_float(r.total_monthly_usd),
        ]
        for r in report.summary_rows
    ]
    return format_table(headers, rows)


def build_cost_breakdown_table(report: ScenarioReport) -> str:
    headers = [
        "Cameras",
        "Fixed USD",
        "GPU USD",
        "Hot USD",
        "Warm USD",
        "Cold USD",
        "Kafka USD",
        "TSDB USD",
        "MTMC USD",
        "Annot USD",
        "Shadow USD",
        "Total USD",
    ]
    rows = [
        [
            str(r.cameras),
            format_float(r.fixed_infrastructure_usd),
            format_float(r.gpu_usd),
            format_float(r.hot_object_usd),
            format_float(r.warm_object_usd),
            format_float(r.cold_object_usd),
            format_float(r.kafka_storage_usd),
            format_float(r.timescaledb_storage_usd),
            format_float(r.mtmc_infra_usd),
            format_float(r.annotation_usd),
            format_float(r.shadow_overhead_usd),
            format_float(r.total_monthly_usd),
        ]
        for r in report.summary_rows
    ]
    return format_table(headers, rows)


def build_comparison_table(inputs: CostModelInputs) -> str:
    headers = ["Parameter", "Estimated", "Measured", "Delta", "Source"]
    changed = [
        row for row in inputs.comparison_rows
        if row[1] != row[2]
    ]
    if not changed:
        return "(no parameter changes)"
    rows = [list(r) for r in changed]
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
    total_partitions = sum(t.partitions for t in topics)
    kafka_brokers = inventory.group_counts["kafka_broker"]
    lines = [
        "Cilex Vision Parametric Cost Model v2 — Measured Parameters",
        "",
        f"Inputs: {inputs.params_path}",
        f"Kafka catalog: {topic_count} topics / {total_partitions} partitions"
        f" / {kafka_brokers} brokers from {topics_path}",
        f"Infra components from {compose_path}: {', '.join(inventory.service_names)}",
    ]
    if xlsx_output is not None:
        lines.append(f"Excel workbook: {xlsx_output}")
    lines.append("")

    lines.append("Parameter Changes (estimated vs measured)")
    lines.append(build_comparison_table(inputs))
    lines.append("")

    for report in reports:
        lines.append(
            f"Scenario {report.scenario.name} "
            f"(motion duty cycle {report.scenario.duty_cycle * 100.0:.1f}%"
            f", was {report.scenario.original_estimate * 100.0:.1f}%)"
        )
        lines.append(build_summary_table(report))
        lines.append("")
        lines.append(build_cost_breakdown_table(report))
        lines.append("")

    lines.append("New Cost Categories")
    lines.append(
        f"  MTMC:       Re-ID GPU fraction {inputs.reid_gpu_fraction:.0%}"
        f", FAISS {inputs.faiss_memory_mb_per_10k_embeddings:.0f} MB/10k embeddings"
        f", checkpoint {inputs.mtmc_checkpoint_storage_mb:.0f} MB"
    )
    lines.append(
        f"  Annotation: CVAT ${inputs.cvat_hosting_monthly_usd:.0f}/mo"
        f" + mining ${inputs.mining_compute_monthly_usd:.0f}/mo"
        f" + annotator ${inputs.annotator_cost_per_hour_usd:.0f}/hr"
        f" x {inputs.daily_mining_hours:.1f} hr/day"
    )
    lines.append(
        f"  Shadow:     GPU +{inputs.shadow_gpu_overhead_factor:.0%}"
        f", Kafka +{inputs.shadow_kafka_overhead_factor:.0%}"
        f" (amortized 1 month/quarter)"
    )
    lines.append("")

    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------------
# Excel output
# ---------------------------------------------------------------------------


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

    # Comparison sheet
    cmp_sheet = workbook.create_sheet("comparison")
    cmp_sheet.append(["Parameter", "Estimated", "Measured", "Delta", "Source"])
    for cell in cmp_sheet[1]:
        cell.font = header_font
    for row in inputs.comparison_rows:
        if row[1] != row[2]:
            cmp_sheet.append(list(row))
    autosize_openpyxl_columns(cmp_sheet)
    cmp_sheet.freeze_panes = "A2"

    # Scenario sheets
    for report in reports:
        sheet = workbook.create_sheet(report.scenario.name.lower())
        sheet.append([
            f"Scenario {report.scenario.name}",
            report.scenario.duty_cycle,
            f"was {report.scenario.original_estimate}",
            report.scenario.source,
        ])
        sheet.append([])

        summary_headers = [
            "Cameras", "Duty", "Raw/day GB", "Central/day GB", "Central FPS",
            "GPU Nodes", "Events/day", "Hot Object GB", "Warm Object GB",
            "Cold Object GB", "Kafka GB", "Timescale GB", "Monthly USD",
        ]
        sheet.append(summary_headers)
        for cell in sheet[sheet.max_row]:
            cell.font = header_font
        for r in report.summary_rows:
            sheet.append([
                r.cameras, r.duty_cycle, r.raw_source_gb_day, r.central_load_gb_day,
                r.central_inference_fps, r.gpu_nodes_needed, r.event_clips_day,
                r.hot_object_storage_gb, r.warm_object_storage_gb,
                r.cold_object_storage_gb, r.kafka_storage_gb,
                r.timescaledb_storage_gb, r.total_monthly_usd,
            ])
        sheet.append([])

        cost_headers = [
            "Cameras", "Fixed USD", "GPU USD", "Hot USD", "Warm USD",
            "Cold USD", "Kafka USD", "TSDB USD", "MTMC USD", "Annot USD",
            "Shadow USD", "Total USD",
        ]
        sheet.append(cost_headers)
        for cell in sheet[sheet.max_row]:
            cell.font = header_font
        for r in report.summary_rows:
            sheet.append([
                r.cameras, r.fixed_infrastructure_usd, r.gpu_usd,
                r.hot_object_usd, r.warm_object_usd, r.cold_object_usd,
                r.kafka_storage_usd, r.timescaledb_storage_usd,
                r.mtmc_infra_usd, r.annotation_usd, r.shadow_overhead_usd,
                r.total_monthly_usd,
            ])
        sheet.append([])

        topic_headers = [
            "Cameras", "Topic", "Cleanup", "Partitions",
            "Retention Days", "Messages/Day", "Steady-State GB",
        ]
        sheet.append(topic_headers)
        for cell in sheet[sheet.max_row]:
            cell.font = header_font
        for sr in report.summary_rows:
            for tr in sr.topic_storage_rows:
                sheet.append([
                    sr.cameras, tr.topic, tr.cleanup_policy, tr.partitions,
                    tr.retention_days, tr.message_volume_per_day, tr.steady_state_gb,
                ])
        sheet.freeze_panes = "A3"
        autosize_openpyxl_columns(sheet)

    path.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(path)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--params", type=Path, default=DEFAULT_PARAMS_PATH,
        help="YAML file with measured cost-model inputs.",
    )
    parser.add_argument(
        "--topics-file", type=Path, default=DEFAULT_TOPICS_PATH,
        help="Kafka topic catalog.",
    )
    parser.add_argument(
        "--compose-file", type=Path, default=DEFAULT_COMPOSE_PATH,
        help="Docker Compose file for fixed infrastructure.",
    )
    parser.add_argument(
        "--xlsx-output", type=Path, default=DEFAULT_XLSX_PATH,
        help="Excel workbook output path.",
    )
    parser.add_argument(
        "--skip-xlsx", action="store_true",
        help="Skip workbook generation.",
    )
    return parser.parse_args()


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
    except Exception as exc:  # pragma: no cover
        raise SystemExit(str(exc)) from exc
