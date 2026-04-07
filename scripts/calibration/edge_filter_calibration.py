#!/usr/bin/env python3
"""Measure and calibrate edge motion-filter pass-through for a single camera.

The script supports two modes:

- live capture: publish a calibration command on NATS, collect `FrameRef`
  messages for a fixed window, download the referenced frames, then analyse
  them centrally
- analysis-only: reuse a prior capture manifest and rerun detector + motion
  calibration without touching NATS

The repository does not ship a live NATS stack, Triton instance, or eval data.
This harness is therefore strict about missing prerequisites and fails fast with
clear install / environment hints.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import logging
import math
import ssl
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_WINDOW_S = 600
DEFAULT_TARGET_PASS_THROUGH_RATE = 0.15
DEFAULT_PASS_THROUGH_TOLERANCE = 0.05
DEFAULT_OUTPUT_DIR = REPO_ROOT / "artifacts" / "calibration"
DEFAULT_PARAMS_PATH = REPO_ROOT / "scripts" / "cost-model" / "params.yaml"
DEFAULT_METRICS_OUTPUT = DEFAULT_OUTPUT_DIR / "prometheus" / "edge_filter_calibration.prom"
DEFAULT_LIVE_SUBJECT_TEMPLATE = "frames.live.{site_id}.{camera_id}"
DEFAULT_CONTROL_SUBJECT_TEMPLATE = "site.{site_id}.control.{camera_id}.calibrate"
OBJECT_CLASSES: tuple[str, ...] = (
    "person",
    "car",
    "truck",
    "bus",
    "bicycle",
    "motorcycle",
    "animal",
)

LOGGER = logging.getLogger("edge_filter_calibration")
_PROTO_TEMP_DIR: tempfile.TemporaryDirectory[str] | None = None


class CameraConfig(BaseModel):
    camera_id: str
    rtsp_url: str
    enabled: bool = True


class NatsTlsConfig(BaseModel):
    cert_file: str
    key_file: str
    ca_file: str


class NatsConfig(BaseModel):
    url: str = "nats://localhost:4222"
    tls: NatsTlsConfig | None = None


class MinioConfig(BaseModel):
    endpoint: str = "localhost:9000"
    access_key: str = "minioadmin"
    secret_key: str = "minioadmin"
    bucket: str = "frame-blobs"
    secure: bool = False


class MotionConfig(BaseModel):
    pixel_threshold: int = 25
    motion_threshold: float = 0.02
    scene_change_threshold: float = 0.80
    reference_update_interval_s: int = 300


class EdgeSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="EDGE_")

    site_id: str = "site-a"
    cameras: list[CameraConfig] = Field(default_factory=list)
    nats: NatsConfig = Field(default_factory=NatsConfig)
    minio: MinioConfig = Field(default_factory=MinioConfig)
    motion: MotionConfig = Field(default_factory=MotionConfig)

    @classmethod
    def from_yaml(cls, path: str | Path) -> "EdgeSettings":
        payload = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        return cls(**payload)


class TritonConfig(BaseModel):
    url: str = "localhost:8001"
    detector_model: str = "yolov8l"
    detector_input_name: str = "images"
    detector_output_name: str = "output0"


class DetectorConfig(BaseModel):
    confidence_threshold: float = 0.40
    nms_iou_threshold: float = 0.45
    input_size: int = 640
    num_classes: int = 7


class InferenceSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="INFERENCE_",
        env_nested_delimiter="__",
    )

    triton: TritonConfig = Field(default_factory=TritonConfig)
    detector: DetectorConfig = Field(default_factory=DetectorConfig)
    minio: MinioConfig = Field(default_factory=MinioConfig)

    @classmethod
    def from_yaml(cls, path: str | Path) -> "InferenceSettings":
        payload = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        return cls(**payload)


@dataclass(frozen=True)
class RunPaths:
    run_id: str
    run_dir: Path
    frames_dir: Path
    capture_manifest_path: Path
    scorecard_json_path: Path
    scorecard_markdown_path: Path


@dataclass(frozen=True)
class FrameEnvelope:
    frame_id: str
    camera_id: str
    frame_uri: str
    frame_sequence: int
    source_capture_ts: float
    edge_receive_ts: float
    width_px: int
    height_px: int
    codec: str
    local_path: Path | None = None

    def to_manifest_dict(self, *, manifest_dir: Path) -> dict[str, Any]:
        local_path_value: str | None = None
        if self.local_path is not None:
            try:
                local_path_value = str(self.local_path.relative_to(manifest_dir))
            except ValueError:
                local_path_value = str(self.local_path)
        return {
            "frame_id": self.frame_id,
            "camera_id": self.camera_id,
            "frame_uri": self.frame_uri,
            "frame_sequence": self.frame_sequence,
            "source_capture_ts": self.source_capture_ts,
            "edge_receive_ts": self.edge_receive_ts,
            "width_px": self.width_px,
            "height_px": self.height_px,
            "codec": self.codec,
            "local_path": local_path_value,
        }


@dataclass(frozen=True)
class CandidateMetrics:
    total_frames: int
    object_positive_frames: int
    object_negative_frames: int
    motion_positive_frames: int
    true_positive_frames: int
    true_negative_frames: int
    false_positive_frames: int
    false_negative_frames: int
    scene_change_frames: int
    miss_rate: float
    false_trigger_rate: float
    pass_through_rate: float
    scene_change_rate: float


@dataclass(frozen=True)
class CandidateResult:
    motion_config: MotionConfig
    metrics: CandidateMetrics
    score: float
    is_baseline: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "motion_config": self.motion_config.model_dump(),
            "metrics": {
                "total_frames": self.metrics.total_frames,
                "object_positive_frames": self.metrics.object_positive_frames,
                "object_negative_frames": self.metrics.object_negative_frames,
                "motion_positive_frames": self.metrics.motion_positive_frames,
                "true_positive_frames": self.metrics.true_positive_frames,
                "true_negative_frames": self.metrics.true_negative_frames,
                "false_positive_frames": self.metrics.false_positive_frames,
                "false_negative_frames": self.metrics.false_negative_frames,
                "scene_change_frames": self.metrics.scene_change_frames,
                "miss_rate": self.metrics.miss_rate,
                "false_trigger_rate": self.metrics.false_trigger_rate,
                "pass_through_rate": self.metrics.pass_through_rate,
                "scene_change_rate": self.metrics.scene_change_rate,
            },
            "score": self.score,
            "is_baseline": self.is_baseline,
        }


class CandidateAccumulator:
    def __init__(self, motion_module: Any, motion_config: MotionConfig, *, is_baseline: bool) -> None:
        self.motion_config = motion_config
        self.is_baseline = is_baseline
        self.detector = motion_module.MotionDetector(
            pixel_threshold=motion_config.pixel_threshold,
            motion_threshold=motion_config.motion_threshold,
            scene_change_threshold=motion_config.scene_change_threshold,
            reference_update_interval_s=motion_config.reference_update_interval_s,
        )
        self.total_frames = 0
        self.object_positive_frames = 0
        self.object_negative_frames = 0
        self.motion_positive_frames = 0
        self.true_positive_frames = 0
        self.true_negative_frames = 0
        self.false_positive_frames = 0
        self.false_negative_frames = 0
        self.scene_change_frames = 0

    def observe(self, frame_gray: np.ndarray, *, object_present: bool) -> None:
        has_motion, is_scene_change = self.detector.detect(frame_gray)
        self.total_frames += 1
        if object_present:
            self.object_positive_frames += 1
        else:
            self.object_negative_frames += 1

        if has_motion:
            self.motion_positive_frames += 1
            if object_present:
                self.true_positive_frames += 1
            else:
                self.false_positive_frames += 1
        else:
            if object_present:
                self.false_negative_frames += 1
            else:
                self.true_negative_frames += 1

        if is_scene_change:
            self.scene_change_frames += 1

    def finalize(self, *, target_pass_through_rate: float) -> CandidateResult:
        total = max(self.total_frames, 1)
        positives = self.object_positive_frames
        negatives = self.object_negative_frames
        miss_rate = self.false_negative_frames / positives if positives else 0.0
        false_trigger_rate = self.false_positive_frames / negatives if negatives else 0.0
        pass_through_rate = self.motion_positive_frames / total
        scene_change_rate = self.scene_change_frames / total
        normalized_distance = min(
            abs(pass_through_rate - target_pass_through_rate) / max(target_pass_through_rate, 1e-9),
            1.0,
        )
        score = (
            0.60 * (1.0 - miss_rate)
            + 0.25 * (1.0 - false_trigger_rate)
            + 0.15 * (1.0 - normalized_distance)
        )
        metrics = CandidateMetrics(
            total_frames=self.total_frames,
            object_positive_frames=self.object_positive_frames,
            object_negative_frames=self.object_negative_frames,
            motion_positive_frames=self.motion_positive_frames,
            true_positive_frames=self.true_positive_frames,
            true_negative_frames=self.true_negative_frames,
            false_positive_frames=self.false_positive_frames,
            false_negative_frames=self.false_negative_frames,
            scene_change_frames=self.scene_change_frames,
            miss_rate=miss_rate,
            false_trigger_rate=false_trigger_rate,
            pass_through_rate=pass_through_rate,
            scene_change_rate=scene_change_rate,
        )
        return CandidateResult(
            motion_config=self.motion_config,
            metrics=metrics,
            score=score,
            is_baseline=self.is_baseline,
        )


def require_module(module_name: str, install_hint: str) -> Any:
    try:
        return importlib.import_module(module_name)
    except ImportError as exc:  # pragma: no cover - depends on local env
        raise RuntimeError(f"missing optional dependency '{module_name}'; install {install_hint}") from exc


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--site-id", help="Site identifier. Defaults to the edge config site_id.")
    parser.add_argument("--camera-id", help="Camera identifier. Required when edge config has multiple cameras.")
    parser.add_argument(
        "--edge-config",
        type=Path,
        help="Edge-agent YAML config used for site/NATS/MinIO/motion defaults and camera discovery.",
    )
    parser.add_argument(
        "--inference-config",
        type=Path,
        help="Inference-worker YAML config used for Triton detector settings.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="Base directory for calibration artifacts.",
    )
    parser.add_argument(
        "--run-id",
        help="Optional deterministic run identifier. Defaults to the current UTC timestamp.",
    )
    parser.add_argument(
        "--capture-window-s",
        type=int,
        default=DEFAULT_WINDOW_S,
        help="Live capture duration in seconds.",
    )
    parser.add_argument(
        "--capture-manifest",
        type=Path,
        help="Reuse an existing capture manifest for analysis-only mode.",
    )
    parser.add_argument(
        "--analysis-only",
        action="store_true",
        help="Skip NATS capture and analyse an existing capture manifest.",
    )
    parser.add_argument(
        "--skip-params-update",
        action="store_true",
        help="Do not write results back into scripts/cost-model/params.yaml.",
    )
    parser.add_argument(
        "--params-yaml",
        type=Path,
        default=DEFAULT_PARAMS_PATH,
        help="YAML file updated with per-camera pass-through measurements.",
    )
    parser.add_argument(
        "--metrics-output",
        type=Path,
        default=DEFAULT_METRICS_OUTPUT,
        help="Prometheus textfile output path for per-camera calibration metrics.",
    )
    parser.add_argument(
        "--target-pass-through-rate",
        type=float,
        default=DEFAULT_TARGET_PASS_THROUGH_RATE,
        help="Desired steady-state motion-filter pass-through rate.",
    )
    parser.add_argument(
        "--pass-through-tolerance",
        type=float,
        default=DEFAULT_PASS_THROUGH_TOLERANCE,
        help="Preferred absolute tolerance around the target pass-through rate.",
    )
    parser.add_argument(
        "--pixel-thresholds",
        nargs="+",
        type=int,
        help="Explicit grid values for MotionConfig.pixel_threshold.",
    )
    parser.add_argument(
        "--motion-thresholds",
        nargs="+",
        type=float,
        help="Explicit grid values for MotionConfig.motion_threshold.",
    )
    parser.add_argument(
        "--scene-change-thresholds",
        nargs="+",
        type=float,
        help="Explicit grid values for MotionConfig.scene_change_threshold.",
    )
    parser.add_argument(
        "--reference-update-intervals",
        nargs="+",
        type=int,
        help="Explicit grid values for MotionConfig.reference_update_interval_s.",
    )
    parser.add_argument(
        "--live-subject-template",
        default=DEFAULT_LIVE_SUBJECT_TEMPLATE,
        help="NATS subject template used to collect live frames.",
    )
    parser.add_argument(
        "--control-subject-template",
        default=DEFAULT_CONTROL_SUBJECT_TEMPLATE,
        help="NATS control subject template used to request calibration mode.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        help="Python logging level.",
    )
    return parser.parse_args()


def load_edge_settings(path: Path | None) -> EdgeSettings:
    if path is None:
        return EdgeSettings()
    if not path.exists():
        raise FileNotFoundError(f"edge config not found: {path}")
    return EdgeSettings.from_yaml(path)


def load_inference_settings(path: Path | None, edge_settings: EdgeSettings) -> InferenceSettings:
    if path is not None:
        if not path.exists():
            raise FileNotFoundError(f"inference config not found: {path}")
        return InferenceSettings.from_yaml(path)
    return InferenceSettings(minio=edge_settings.minio.model_copy())


def resolve_camera(edge_settings: EdgeSettings, requested_camera_id: str | None) -> CameraConfig:
    enabled_cameras = [camera for camera in edge_settings.cameras if camera.enabled]
    if requested_camera_id is not None:
        for camera in edge_settings.cameras:
            if camera.camera_id == requested_camera_id:
                if not camera.enabled:
                    raise ValueError(f"camera is disabled in edge config: {requested_camera_id}")
                return camera
        if edge_settings.cameras:
            raise ValueError(f"camera_id not found in edge config: {requested_camera_id}")
        return CameraConfig(camera_id=requested_camera_id, rtsp_url="")

    if len(enabled_cameras) == 1:
        return enabled_cameras[0]
    if not enabled_cameras:
        raise ValueError("camera_id is required because the edge config does not define any enabled cameras")
    raise ValueError("camera_id is required because the edge config defines multiple enabled cameras")


def camera_key(site_id: str, camera_id: str) -> str:
    return f"{site_id}/{camera_id}"


def current_utc_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")


def iso_to_epoch(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def default_run_id() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def build_run_paths(
    *,
    output_dir: Path,
    site_id: str,
    camera_id: str,
    run_id: str | None,
    capture_manifest: Path | None,
    analysis_only: bool,
) -> RunPaths:
    if analysis_only and capture_manifest is not None:
        run_dir = capture_manifest.resolve().parent
        actual_run_id = run_dir.name
        return RunPaths(
            run_id=actual_run_id,
            run_dir=run_dir,
            frames_dir=run_dir / "frames",
            capture_manifest_path=capture_manifest.resolve(),
            scorecard_json_path=run_dir / "scorecard.json",
            scorecard_markdown_path=run_dir / "scorecard.md",
        )

    actual_run_id = run_id or default_run_id()
    run_dir = output_dir / site_id / camera_id / actual_run_id
    return RunPaths(
        run_id=actual_run_id,
        run_dir=run_dir,
        frames_dir=run_dir / "frames",
        capture_manifest_path=(capture_manifest or run_dir / "capture-manifest.json").resolve(),
        scorecard_json_path=(run_dir / "scorecard.json").resolve(),
        scorecard_markdown_path=(run_dir / "scorecard.md").resolve(),
    )


def parse_s3_uri(uri: str) -> tuple[str, str]:
    raw = uri
    if uri.startswith("s3://"):
        raw = uri[5:]
    elif uri.startswith("minio://"):
        raw = uri[8:]
    bucket, _, object_name = raw.partition("/")
    if not bucket or not object_name:
        raise ValueError(f"invalid object-store URI: {uri}")
    return bucket, object_name


def ensure_frame_ref_module() -> Any:
    global _PROTO_TEMP_DIR

    candidate_dirs = [
        REPO_ROOT / "services" / "edge-agent" / "proto_gen",
        REPO_ROOT / "services" / "decode-service" / "proto_gen",
        REPO_ROOT / "services" / "ingress-bridge" / "proto_gen",
    ]
    for candidate in candidate_dirs:
        module_path = candidate / "vidanalytics" / "v1" / "frame" / "frame_pb2.py"
        if module_path.exists():
            if str(candidate) not in sys.path:
                sys.path.insert(0, str(candidate))
            return importlib.import_module("vidanalytics.v1.frame.frame_pb2")

    try:
        from grpc_tools import protoc
    except ImportError as exc:  # pragma: no cover - depends on local env
        raise RuntimeError(
            "generated FrameRef protobufs are unavailable; run "
            "`bash services/edge-agent/gen_proto.sh` or install grpcio-tools"
        ) from exc

    _PROTO_TEMP_DIR = tempfile.TemporaryDirectory(prefix="edge-filter-proto-")
    proto_out = Path(_PROTO_TEMP_DIR.name)
    proto_root = REPO_ROOT / "proto"
    result = protoc.main(
        [
            "grpc_tools.protoc",
            f"-I{proto_root}",
            f"--python_out={proto_out}",
            str(proto_root / "vidanalytics/v1/common/common.proto"),
            str(proto_root / "vidanalytics/v1/frame/frame.proto"),
        ]
    )
    if result != 0:
        raise RuntimeError("failed to generate FrameRef protobufs with grpc_tools.protoc")
    for directory in proto_out.rglob("*"):
        if directory.is_dir():
            (directory / "__init__.py").touch(exist_ok=True)
    if str(proto_out) not in sys.path:
        sys.path.insert(0, str(proto_out))
    return importlib.import_module("vidanalytics.v1.frame.frame_pb2")


def timestamp_to_epoch(timestamp: Any) -> float:
    if not getattr(timestamp, "ListFields", None) or not timestamp.ListFields():
        raise ValueError("protobuf timestamp field is missing")
    return float(timestamp.seconds) + float(timestamp.nanos) / 1_000_000_000.0


def decode_frame_ref(payload: bytes) -> FrameEnvelope:
    frame_pb2 = ensure_frame_ref_module()
    message = frame_pb2.FrameRef()
    message.ParseFromString(payload)
    if not message.frame_id:
        raise ValueError("FrameRef missing frame_id")
    if not message.camera_id:
        raise ValueError("FrameRef missing camera_id")
    if not message.frame_uri:
        raise ValueError("FrameRef missing frame_uri")
    timestamps = message.timestamps
    return FrameEnvelope(
        frame_id=str(message.frame_id),
        camera_id=str(message.camera_id),
        frame_uri=str(message.frame_uri),
        frame_sequence=int(message.frame_sequence),
        source_capture_ts=timestamp_to_epoch(timestamps.source_capture_ts),
        edge_receive_ts=timestamp_to_epoch(timestamps.edge_receive_ts),
        width_px=int(message.width_px),
        height_px=int(message.height_px),
        codec=str(message.codec or "jpeg"),
    )


def build_tls_context(tls_config: NatsTlsConfig | None) -> ssl.SSLContext | None:
    if tls_config is None:
        return None
    context = ssl.create_default_context(purpose=ssl.Purpose.SERVER_AUTH)
    context.load_verify_locations(tls_config.ca_file)
    context.load_cert_chain(tls_config.cert_file, tls_config.key_file)
    return context


async def capture_frames_live(
    *,
    site_id: str,
    camera_id: str,
    edge_settings: EdgeSettings,
    capture_window_s: int,
    live_subject_template: str,
    control_subject_template: str,
) -> list[FrameEnvelope]:
    nats = require_module("nats", "nats-py")

    live_subject = live_subject_template.format(site_id=site_id, camera_id=camera_id)
    control_subject = control_subject_template.format(site_id=site_id, camera_id=camera_id)
    tls_context = build_tls_context(edge_settings.nats.tls)
    frames: list[FrameEnvelope] = []
    errors: list[str] = []

    nc = await nats.connect(edge_settings.nats.url, tls=tls_context)

    async def _on_message(message: Any) -> None:
        try:
            frame = decode_frame_ref(message.data)
        except Exception as exc:  # pragma: no cover - depends on live traffic
            errors.append(str(exc))
            return
        if frame.camera_id != camera_id:
            errors.append(f"unexpected camera_id on {live_subject}: {frame.camera_id}")
            return
        frames.append(frame)

    LOGGER.info("Subscribing to %s", live_subject)
    subscription = await nc.subscribe(live_subject, cb=_on_message)
    await nc.flush()

    command_payload = {
        "command": "calibrate",
        "site_id": site_id,
        "camera_id": camera_id,
        "disable_motion_filtering": True,
        "requested_window_s": capture_window_s,
        "issued_at": current_utc_iso(),
    }
    LOGGER.info("Publishing calibration command on %s", control_subject)
    await nc.publish(control_subject, json.dumps(command_payload, sort_keys=True).encode("utf-8"))
    await nc.flush()

    LOGGER.info("Collecting frames for %s seconds", capture_window_s)
    await asyncio.sleep(capture_window_s)
    await subscription.unsubscribe()
    await nc.drain()

    if errors:
        LOGGER.warning("capture encountered %s malformed frame message(s)", len(errors))
    if not frames:
        raise RuntimeError(
            f"no frames were captured on {live_subject}. "
            "Verify the edge agent is publishing all frames during calibration mode."
        )
    unique: dict[str, FrameEnvelope] = {frame.frame_id: frame for frame in frames}
    ordered = sorted(
        unique.values(),
        key=lambda frame: (frame.edge_receive_ts, frame.frame_sequence, frame.frame_id),
    )
    return ordered


def create_minio_client(minio_config: MinioConfig) -> Any:
    minio_module = require_module("minio", "minio")
    return minio_module.Minio(
        minio_config.endpoint,
        access_key=minio_config.access_key,
        secret_key=minio_config.secret_key,
        secure=minio_config.secure,
    )


def read_object_bytes(minio_client: Any, frame_uri: str) -> bytes:
    bucket, object_name = parse_s3_uri(frame_uri)
    response = minio_client.get_object(bucket, object_name)
    try:
        data = response.read()
    finally:
        response.close()
        response.release_conn()
    return data


def frame_file_suffix(codec: str) -> str:
    codec_value = codec.lower()
    if codec_value in {"jpeg", "jpg"}:
        return ".jpg"
    if codec_value == "png":
        return ".png"
    return ".bin"


async def ensure_local_frames(records: list[FrameEnvelope], *, frames_dir: Path, minio_config: MinioConfig) -> list[FrameEnvelope]:
    frames_dir.mkdir(parents=True, exist_ok=True)
    resolved: list[FrameEnvelope] = []

    missing = [record for record in records if record.local_path is None or not Path(record.local_path).exists()]
    minio_client: Any | None = None
    if missing:
        minio_client = create_minio_client(minio_config)

    for record in records:
        if record.local_path is not None and record.local_path.exists():
            resolved.append(record)
            continue
        assert minio_client is not None
        suffix = frame_file_suffix(record.codec)
        local_path = frames_dir / f"{record.frame_id}{suffix}"
        data = await asyncio.to_thread(read_object_bytes, minio_client, record.frame_uri)
        local_path.write_bytes(data)
        resolved.append(
            FrameEnvelope(
                frame_id=record.frame_id,
                camera_id=record.camera_id,
                frame_uri=record.frame_uri,
                frame_sequence=record.frame_sequence,
                source_capture_ts=record.source_capture_ts,
                edge_receive_ts=record.edge_receive_ts,
                width_px=record.width_px,
                height_px=record.height_px,
                codec=record.codec,
                local_path=local_path,
            )
        )
    return resolved


def write_capture_manifest(
    path: Path,
    *,
    site_id: str,
    camera_id: str,
    run_id: str,
    capture_window_s: int,
    live_subject_template: str,
    control_subject_template: str,
    frames: list[FrameEnvelope],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "task_id": "P1-E03",
        "site_id": site_id,
        "camera_id": camera_id,
        "run_id": run_id,
        "capture_window_s": capture_window_s,
        "live_subject": live_subject_template.format(site_id=site_id, camera_id=camera_id),
        "control_subject": control_subject_template.format(site_id=site_id, camera_id=camera_id),
        "captured_at": current_utc_iso(),
        "frames": [
            frame.to_manifest_dict(manifest_dir=path.parent)
            for frame in frames
        ],
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def load_capture_manifest(path: Path) -> tuple[str, str, str, int, list[FrameEnvelope]]:
    if not path.exists():
        raise FileNotFoundError(f"capture manifest not found: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    frames_raw = payload.get("frames")
    if not isinstance(frames_raw, list) or not frames_raw:
        raise ValueError("capture manifest must contain a non-empty frames array")
    site_id = str(payload.get("site_id") or "")
    camera_id = str(payload.get("camera_id") or "")
    run_id = str(payload.get("run_id") or path.parent.name)
    capture_window_s = int(payload.get("capture_window_s") or 0)
    records: list[FrameEnvelope] = []
    for raw in frames_raw:
        local_path = raw.get("local_path")
        resolved_local_path = None
        if local_path:
            local_candidate = Path(str(local_path))
            resolved_local_path = local_candidate if local_candidate.is_absolute() else (path.parent / local_candidate).resolve()
        records.append(
            FrameEnvelope(
                frame_id=str(raw["frame_id"]),
                camera_id=str(raw["camera_id"]),
                frame_uri=str(raw["frame_uri"]),
                frame_sequence=int(raw["frame_sequence"]),
                source_capture_ts=float(raw["source_capture_ts"]),
                edge_receive_ts=float(raw["edge_receive_ts"]),
                width_px=int(raw["width_px"]),
                height_px=int(raw["height_px"]),
                codec=str(raw.get("codec") or "jpeg"),
                local_path=resolved_local_path,
            )
        )
    records = sorted(records, key=lambda frame: (frame.edge_receive_ts, frame.frame_sequence, frame.frame_id))
    return site_id, camera_id, run_id, capture_window_s, records


def load_motion_module() -> Any:
    edge_dir = REPO_ROOT / "services" / "edge-agent"
    if str(edge_dir) not in sys.path:
        sys.path.insert(0, str(edge_dir))
    return importlib.import_module("motion_detector")


def load_detector_module() -> Any:
    worker_dir = REPO_ROOT / "services" / "inference-worker"
    if str(worker_dir) not in sys.path:
        sys.path.insert(0, str(worker_dir))
    return importlib.import_module("detector_client")


def build_motion_grid(
    base_motion: MotionConfig,
    args: argparse.Namespace,
) -> list[MotionConfig]:
    pixel_thresholds = sorted(
        {
            *(args.pixel_thresholds or []),
            max(1, base_motion.pixel_threshold - 5),
            base_motion.pixel_threshold,
            base_motion.pixel_threshold + 5,
        }
    )
    motion_thresholds = sorted(
        {
            *(args.motion_thresholds or []),
            max(0.005, round(base_motion.motion_threshold * 0.5, 4)),
            max(0.005, round(base_motion.motion_threshold * 0.75, 4)),
            round(base_motion.motion_threshold, 4),
            round(base_motion.motion_threshold * 1.5, 4),
            round(base_motion.motion_threshold * 2.0, 4),
        }
    )
    scene_change_thresholds = sorted(
        {
            *(args.scene_change_thresholds or []),
            round(max(0.50, base_motion.scene_change_threshold - 0.10), 4),
            round(base_motion.scene_change_threshold, 4),
            round(min(0.99, base_motion.scene_change_threshold + 0.10), 4),
        }
    )
    reference_update_intervals = sorted(
        {
            *(args.reference_update_intervals or []),
            max(30, base_motion.reference_update_interval_s // 2),
            base_motion.reference_update_interval_s,
            max(30, base_motion.reference_update_interval_s * 2),
        }
    )

    deduped: dict[tuple[int, float, float, int], MotionConfig] = {}
    for pixel_threshold in pixel_thresholds:
        for motion_threshold in motion_thresholds:
            for scene_change_threshold in scene_change_thresholds:
                if scene_change_threshold <= motion_threshold:
                    continue
                for reference_update_interval_s in reference_update_intervals:
                    config = MotionConfig(
                        pixel_threshold=int(pixel_threshold),
                        motion_threshold=float(motion_threshold),
                        scene_change_threshold=float(scene_change_threshold),
                        reference_update_interval_s=int(reference_update_interval_s),
                    )
                    key = (
                        config.pixel_threshold,
                        round(config.motion_threshold, 6),
                        round(config.scene_change_threshold, 6),
                        config.reference_update_interval_s,
                    )
                    deduped[key] = config
    baseline_key = (
        base_motion.pixel_threshold,
        round(base_motion.motion_threshold, 6),
        round(base_motion.scene_change_threshold, 6),
        base_motion.reference_update_interval_s,
    )
    deduped[baseline_key] = base_motion
    return list(deduped.values())


def choose_recommended_candidate(
    results: list[CandidateResult],
    *,
    target_pass_through_rate: float,
    pass_through_tolerance: float,
) -> tuple[CandidateResult, str]:
    baseline = next(result for result in results if result.is_baseline)
    preferred = [
        result
        for result in results
        if abs(result.metrics.pass_through_rate - target_pass_through_rate) <= pass_through_tolerance
    ]
    if preferred:
        pool = preferred
        note = (
            f"Preferred candidates were limited to pass-through within ±{pass_through_tolerance:.3f} "
            f"of the {target_pass_through_rate:.3f} target."
        )
    else:
        pool = results
        note = (
            f"No candidate landed within ±{pass_through_tolerance:.3f} of the {target_pass_through_rate:.3f} "
            "target, so the full grid was ranked."
        )

    recommended = sorted(
        pool,
        key=lambda result: (
            -result.score,
            result.metrics.miss_rate,
            result.metrics.false_trigger_rate,
            abs(result.metrics.pass_through_rate - target_pass_through_rate),
            0 if result.is_baseline else 1,
        ),
    )[0]

    if math.isclose(recommended.score, baseline.score, rel_tol=0.0, abs_tol=1e-9):
        recommended = baseline
        note = note + " Baseline retained because it tied the best candidate."
    return recommended, note


async def load_rgb_image(path: Path) -> np.ndarray:
    from PIL import Image

    def _read() -> np.ndarray:
        image = Image.open(path).convert("RGB")
        return np.array(image)

    return await asyncio.to_thread(_read)


async def analyse_capture(
    *,
    frames: list[FrameEnvelope],
    inference_settings: InferenceSettings,
    base_motion: MotionConfig,
    args: argparse.Namespace,
) -> tuple[CandidateResult, CandidateResult, list[CandidateResult], dict[str, int], dict[str, Any]]:
    if not frames:
        raise ValueError("no frames available for analysis")

    require_module("tritonclient.grpc", "tritonclient[grpc]")
    detector_module = load_detector_module()
    detector_client = detector_module.DetectorClient(
        inference_settings.triton,
        inference_settings.detector,
    )

    motion_module = load_motion_module()
    motion_grid = build_motion_grid(base_motion, args)
    candidate_runners = [
        CandidateAccumulator(
            motion_module,
            motion_config=motion_config,
            is_baseline=(motion_config == base_motion),
        )
        for motion_config in motion_grid
    ]

    live_client = detector_client._get_client()
    is_live = await asyncio.to_thread(live_client.is_server_live)
    is_ready = await asyncio.to_thread(live_client.is_server_ready)
    if not is_live or not is_ready:
        raise RuntimeError(
            f"Triton is not ready at {inference_settings.triton.url} "
            f"(live={is_live}, ready={is_ready})"
        )

    first_edge_ts = frames[0].edge_receive_ts
    detected_frames_by_class = {class_name: 0 for class_name in OBJECT_CLASSES}
    analysis_started_at = time.monotonic()
    motion_time = motion_module.time
    original_monotonic = motion_time.monotonic

    try:
        for index, frame in enumerate(frames, start=1):
            local_path = frame.local_path
            if local_path is None:
                raise RuntimeError(f"frame {frame.frame_id} is missing a local_path for analysis")
            if not local_path.exists():
                raise FileNotFoundError(f"captured frame file not found: {local_path}")

            rgb = await load_rgb_image(local_path)
            detections = await detector_client.detect(rgb)
            object_present = bool(detections)
            seen_classes = {detection.class_name for detection in detections}
            for class_name in seen_classes:
                if class_name in detected_frames_by_class:
                    detected_frames_by_class[class_name] += 1

            synthetic_monotonic = max(frame.edge_receive_ts - first_edge_ts, 0.0)
            motion_time.monotonic = lambda synthetic_monotonic=synthetic_monotonic: synthetic_monotonic
            gray = motion_module._to_grayscale(rgb)
            for runner in candidate_runners:
                runner.observe(gray, object_present=object_present)

            if index % 250 == 0:
                LOGGER.info("Analysed %s / %s frames", index, len(frames))
    finally:
        motion_time.monotonic = original_monotonic

    analysis_runtime_s = time.monotonic() - analysis_started_at
    candidate_results = [
        runner.finalize(target_pass_through_rate=args.target_pass_through_rate)
        for runner in candidate_runners
    ]
    baseline = next(result for result in candidate_results if result.is_baseline)
    recommended, recommendation_note = choose_recommended_candidate(
        candidate_results,
        target_pass_through_rate=args.target_pass_through_rate,
        pass_through_tolerance=args.pass_through_tolerance,
    )

    detector_summary = {
        "model_name": inference_settings.triton.detector_model,
        "confidence_threshold": inference_settings.detector.confidence_threshold,
        "nms_iou_threshold": inference_settings.detector.nms_iou_threshold,
        "triton_url": inference_settings.triton.url,
        "analysis_runtime_s": analysis_runtime_s,
        "effective_fps": len(frames) / analysis_runtime_s if analysis_runtime_s > 0 else 0.0,
        "recommendation_note": recommendation_note,
    }
    return baseline, recommended, candidate_results, detected_frames_by_class, detector_summary


def build_scorecard_payload(
    *,
    site_id: str,
    camera_id: str,
    run_paths: RunPaths,
    capture_window_s: int,
    baseline: CandidateResult,
    recommended: CandidateResult,
    candidate_results: list[CandidateResult],
    detected_frames_by_class: dict[str, int],
    detector_summary: dict[str, Any],
    metrics_output: Path,
) -> dict[str, Any]:
    return {
        "task_id": "P1-E03",
        "site_id": site_id,
        "camera_id": camera_id,
        "run_id": run_paths.run_id,
        "captured_window_s": capture_window_s,
        "captured_at": current_utc_iso(),
        "capture_manifest_path": str(run_paths.capture_manifest_path),
        "scorecard_json_path": str(run_paths.scorecard_json_path),
        "scorecard_markdown_path": str(run_paths.scorecard_markdown_path),
        "metrics_output_path": str(metrics_output),
        "detector": detector_summary,
        "baseline": baseline.to_dict(),
        "recommended": recommended.to_dict(),
        "candidate_results": [result.to_dict() for result in sorted(candidate_results, key=lambda item: item.score, reverse=True)],
        "detected_frames_by_class": detected_frames_by_class,
    }


def write_markdown_scorecard(path: Path, payload: dict[str, Any]) -> None:
    baseline = payload["baseline"]
    recommended = payload["recommended"]
    detector = payload["detector"]
    top_candidates = payload["candidate_results"][:10]

    def candidate_row(index: int, item: dict[str, Any]) -> str:
        motion = item["motion_config"]
        metrics = item["metrics"]
        variant = "baseline" if item["is_baseline"] else str(index)
        return (
            f"| {variant} | {motion['pixel_threshold']} | {motion['motion_threshold']:.4f} "
            f"| {motion['scene_change_threshold']:.4f} | {motion['reference_update_interval_s']} "
            f"| {metrics['pass_through_rate']:.4f} | {metrics['miss_rate']:.4f} "
            f"| {metrics['false_trigger_rate']:.4f} | {metrics['scene_change_rate']:.4f} "
            f"| {item['score']:.4f} |"
        )

    lines = [
        "# Edge Filter Calibration Scorecard",
        "",
        f"- Site: `{payload['site_id']}`",
        f"- Camera: `{payload['camera_id']}`",
        f"- Run ID: `{payload['run_id']}`",
        f"- Capture window: `{payload['captured_window_s']}` seconds",
        f"- Detector: `{detector['model_name']}` at confidence `{detector['confidence_threshold']:.2f}`",
        f"- Triton: `{detector['triton_url']}`",
        f"- Detector analysis throughput: `{detector['effective_fps']:.2f}` FPS",
        "",
        "## Recommendation",
        "",
        detector["recommendation_note"],
        "",
        "## Baseline vs Recommended",
        "",
        "| Variant | pixel_threshold | motion_threshold | scene_change_threshold | reference_update_interval_s | pass_through_rate | miss_rate | false_trigger_rate | scene_change_rate | score |",
        "| ------- | --------------- | ---------------- | ---------------------- | --------------------------- | ----------------- | --------- | ------------------ | ----------------- | ----- |",
        candidate_row(0, baseline),
    ]
    if recommended["motion_config"] != baseline["motion_config"]:
        lines.append(candidate_row(1, recommended))
    lines.extend(
        [
            "",
            "## Top Candidate Grid",
            "",
            "| Variant | pixel_threshold | motion_threshold | scene_change_threshold | reference_update_interval_s | pass_through_rate | miss_rate | false_trigger_rate | scene_change_rate | score |",
            "| ------- | --------------- | ---------------- | ---------------------- | --------------------------- | ----------------- | --------- | ------------------ | ----------------- | ----- |",
        ]
    )
    for index, item in enumerate(top_candidates, start=1):
        lines.append(candidate_row(index, item))

    lines.extend(
        [
            "",
            "## Detector Label Distribution",
            "",
            "| Class | Frames with Detection |",
            "| ----- | --------------------- |",
        ]
    )
    for class_name in OBJECT_CLASSES:
        lines.append(f"| {class_name} | {payload['detected_frames_by_class'].get(class_name, 0)} |")

    lines.extend(
        [
            "",
            "## Artifacts",
            "",
            f"- Capture manifest: `{payload['capture_manifest_path']}`",
            f"- JSON scorecard: `{payload['scorecard_json_path']}`",
            f"- Metrics textfile: `{payload['metrics_output_path']}`",
            "",
        ]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def load_params_document(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {
            "version": 1,
            "updated_at": None,
            "defaults": {
                "target_pass_through_rate": DEFAULT_TARGET_PASS_THROUGH_RATE,
                "capture_window_s": DEFAULT_WINDOW_S,
                "detector_model": "yolov8l",
                "detector_confidence_threshold": 0.40,
            },
            "cameras": {},
        }
    payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    payload.setdefault("version", 1)
    payload.setdefault("defaults", {})
    payload["defaults"].setdefault("target_pass_through_rate", DEFAULT_TARGET_PASS_THROUGH_RATE)
    payload["defaults"].setdefault("capture_window_s", DEFAULT_WINDOW_S)
    payload["defaults"].setdefault("detector_model", "yolov8l")
    payload["defaults"].setdefault("detector_confidence_threshold", 0.40)
    payload.setdefault("cameras", {})
    return payload


def update_params_document(
    path: Path,
    *,
    site_id: str,
    camera_id: str,
    capture_window_s: int,
    detector_summary: dict[str, Any],
    baseline: CandidateResult,
    recommended: CandidateResult,
    scorecard_json_path: Path,
    capture_manifest_path: Path,
) -> dict[str, Any]:
    payload = load_params_document(path)
    payload["updated_at"] = current_utc_iso()
    payload["defaults"]["target_pass_through_rate"] = DEFAULT_TARGET_PASS_THROUGH_RATE
    payload["defaults"]["capture_window_s"] = capture_window_s
    payload["defaults"]["detector_model"] = detector_summary["model_name"]
    payload["defaults"]["detector_confidence_threshold"] = detector_summary["confidence_threshold"]

    payload["cameras"][camera_key(site_id, camera_id)] = {
        "site_id": site_id,
        "camera_id": camera_id,
        "measured_at": current_utc_iso(),
        "capture_window_s": capture_window_s,
        "pass_through_rate": recommended.metrics.pass_through_rate,
        "miss_rate": recommended.metrics.miss_rate,
        "false_trigger_rate": recommended.metrics.false_trigger_rate,
        "scene_change_rate": recommended.metrics.scene_change_rate,
        "total_frames": recommended.metrics.total_frames,
        "object_positive_frames": recommended.metrics.object_positive_frames,
        "object_negative_frames": recommended.metrics.object_negative_frames,
        "baseline_motion": baseline.motion_config.model_dump(),
        "recommended_motion": recommended.motion_config.model_dump(),
        "detector_model": detector_summary["model_name"],
        "detector_confidence_threshold": detector_summary["confidence_threshold"],
        "scorecard_json_path": str(scorecard_json_path),
        "capture_manifest_path": str(capture_manifest_path),
    }

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return payload


def build_metrics_text(
    params_document: dict[str, Any],
    *,
    inventory: list[tuple[str, str]] | None = None,
    now_epoch: float | None = None,
) -> str:
    now_value = time.time() if now_epoch is None else now_epoch
    cameras_payload = params_document.get("cameras") or {}
    known = set(inventory or [])
    for entry in cameras_payload.values():
        site_id = str(entry.get("site_id") or "")
        camera_id = str(entry.get("camera_id") or "")
        if site_id and camera_id:
            known.add((site_id, camera_id))

    def format_float(value: float) -> str:
        return "NaN" if math.isnan(value) else f"{value:.12g}"

    lines = [
        "# HELP per_camera_pass_through_rate Last measured motion-filter pass-through rate for a camera.",
        "# TYPE per_camera_pass_through_rate gauge",
    ]
    for site_id, camera_id in sorted(known):
        entry = cameras_payload.get(camera_key(site_id, camera_id), {})
        pass_through_rate = float(entry["pass_through_rate"]) if "pass_through_rate" in entry else math.nan
        lines.append(
            f'per_camera_pass_through_rate{{site_id="{site_id}",camera_id="{camera_id}"}} {format_float(pass_through_rate)}'
        )

    lines.extend(
        [
            "# HELP calibration_freshness_hours Hours since the most recent completed calibration for a camera.",
            "# TYPE calibration_freshness_hours gauge",
        ]
    )
    for site_id, camera_id in sorted(known):
        entry = cameras_payload.get(camera_key(site_id, camera_id), {})
        measured_at = entry.get("measured_at")
        freshness_hours = math.nan
        if measured_at:
            freshness_hours = max((now_value - iso_to_epoch(str(measured_at))) / 3600.0, 0.0)
        lines.append(
            f'calibration_freshness_hours{{site_id="{site_id}",camera_id="{camera_id}"}} {format_float(freshness_hours)}'
        )

    return "\n".join(lines) + "\n"


def write_metrics_textfile(
    path: Path,
    params_document: dict[str, Any],
    *,
    inventory: list[tuple[str, str]] | None = None,
    now_epoch: float | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(build_metrics_text(params_document, inventory=inventory, now_epoch=now_epoch), encoding="utf-8")


async def run(args: argparse.Namespace) -> None:
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    edge_settings = load_edge_settings(args.edge_config)
    camera = resolve_camera(edge_settings, args.camera_id)
    site_id = args.site_id or edge_settings.site_id
    run_paths = build_run_paths(
        output_dir=args.output_dir,
        site_id=site_id,
        camera_id=camera.camera_id,
        run_id=args.run_id,
        capture_manifest=args.capture_manifest,
        analysis_only=args.analysis_only,
    )

    if args.analysis_only:
        if args.capture_manifest is None:
            raise ValueError("--analysis-only requires --capture-manifest")
        manifest_site_id, manifest_camera_id, manifest_run_id, capture_window_s, frames = load_capture_manifest(args.capture_manifest)
        if manifest_site_id and manifest_site_id != site_id:
            raise ValueError(f"capture manifest site_id mismatch: expected {site_id}, found {manifest_site_id}")
        if manifest_camera_id and manifest_camera_id != camera.camera_id:
            raise ValueError(
                f"capture manifest camera_id mismatch: expected {camera.camera_id}, found {manifest_camera_id}"
            )
        if manifest_run_id and run_paths.run_id != manifest_run_id:
            run_paths = RunPaths(
                run_id=manifest_run_id,
                run_dir=run_paths.run_dir,
                frames_dir=run_paths.frames_dir,
                capture_manifest_path=run_paths.capture_manifest_path,
                scorecard_json_path=run_paths.scorecard_json_path,
                scorecard_markdown_path=run_paths.scorecard_markdown_path,
            )
    else:
        capture_window_s = args.capture_window_s
        frames = await capture_frames_live(
            site_id=site_id,
            camera_id=camera.camera_id,
            edge_settings=edge_settings,
            capture_window_s=capture_window_s,
            live_subject_template=args.live_subject_template,
            control_subject_template=args.control_subject_template,
        )

    run_paths.run_dir.mkdir(parents=True, exist_ok=True)
    inference_settings = load_inference_settings(args.inference_config, edge_settings)
    frames = await ensure_local_frames(frames, frames_dir=run_paths.frames_dir, minio_config=inference_settings.minio)
    write_capture_manifest(
        run_paths.capture_manifest_path,
        site_id=site_id,
        camera_id=camera.camera_id,
        run_id=run_paths.run_id,
        capture_window_s=capture_window_s,
        live_subject_template=args.live_subject_template,
        control_subject_template=args.control_subject_template,
        frames=frames,
    )

    baseline, recommended, candidate_results, detected_frames_by_class, detector_summary = await analyse_capture(
        frames=frames,
        inference_settings=inference_settings,
        base_motion=edge_settings.motion,
        args=args,
    )

    scorecard_payload = build_scorecard_payload(
        site_id=site_id,
        camera_id=camera.camera_id,
        run_paths=run_paths,
        capture_window_s=capture_window_s,
        baseline=baseline,
        recommended=recommended,
        candidate_results=candidate_results,
        detected_frames_by_class=detected_frames_by_class,
        detector_summary=detector_summary,
        metrics_output=args.metrics_output,
    )
    run_paths.scorecard_json_path.write_text(
        json.dumps(scorecard_payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    write_markdown_scorecard(run_paths.scorecard_markdown_path, scorecard_payload)

    if args.skip_params_update:
        params_document = load_params_document(args.params_yaml)
    else:
        params_document = update_params_document(
            args.params_yaml,
            site_id=site_id,
            camera_id=camera.camera_id,
            capture_window_s=capture_window_s,
            detector_summary=detector_summary,
            baseline=baseline,
            recommended=recommended,
            scorecard_json_path=run_paths.scorecard_json_path,
            capture_manifest_path=run_paths.capture_manifest_path,
        )
    write_metrics_textfile(
        args.metrics_output,
        params_document,
        inventory=[(site_id, camera.camera_id)],
    )

    print(f"Calibration run ID: {run_paths.run_id}")
    print(f"Capture manifest: {run_paths.capture_manifest_path}")
    print(f"JSON scorecard: {run_paths.scorecard_json_path}")
    print(f"Markdown scorecard: {run_paths.scorecard_markdown_path}")
    print(f"Recommended pass-through rate: {recommended.metrics.pass_through_rate:.4f}")
    print(f"Recommended miss rate: {recommended.metrics.miss_rate:.4f}")
    print(f"Recommended false trigger rate: {recommended.metrics.false_trigger_rate:.4f}")
    print(f"Metrics textfile: {args.metrics_output}")


def main() -> None:
    args = parse_args()
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:  # pragma: no cover - CLI boundary
        raise SystemExit(130) from None
    except Exception as exc:  # pragma: no cover - CLI boundary
        raise SystemExit(str(exc)) from exc


if __name__ == "__main__":
    main()
