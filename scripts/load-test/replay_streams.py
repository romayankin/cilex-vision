#!/usr/bin/env python3
"""Replay recorded video streams to simulate 50-100 cameras.

This tool uses FFmpeg to sample frames from a smaller set of source recordings,
then multiplexes those sampled sequences across many virtual camera IDs at a
real-time publish rate. The output path matches the existing load-test harness:
JPEG frames are uploaded to MinIO and `FrameRef` protobuf messages are
published to Kafka.

Usage:
    python replay_streams.py --source-dir recordings/ --cameras 100 \
        --minio-url http://localhost:9000 --kafka-bootstrap localhost:9092
"""

from __future__ import annotations

import argparse
import asyncio
import io
import logging
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from common import load_frame_ref_type, set_proto_timestamp, utc_now  # noqa: E402


LOGGER = logging.getLogger("replay_streams")
SUPPORTED_VIDEO_SUFFIXES = {".avi", ".mkv", ".mov", ".mp4"}
FRAME_PROTO_SCHEMA = b"vidanalytics.v1.frame.FrameRef"


@dataclass(slots=True)
class ReplayConfig:
    """Runtime configuration for scale replay."""

    source_dir: Path
    camera_count: int
    fps: int
    duration_s: int | None
    minio_url: str
    minio_access_key: str
    minio_secret_key: str
    minio_secure: bool
    kafka_bootstrap: str
    site_id: str
    kafka_topic: str = "frames.sampled.refs"
    kafka_security_protocol: str = "PLAINTEXT"
    source_bucket: str = "frame-blobs"
    camera_prefix: str = "cam"
    frame_width_px: int = 1280
    frame_height_px: int = 720
    max_source_seconds: int = 300

    @property
    def camera_ids(self) -> list[str]:
        width = max(3, len(str(self.camera_count)))
        return [
            f"{self.camera_prefix}-{index:0{width}d}"
            for index in range(1, self.camera_count + 1)
        ]


@dataclass(slots=True)
class SourceFrames:
    """Prepared frame cache for one source video."""

    source_path: Path
    frame_paths: list[Path]


class ReplayPublisher:
    """Manage MinIO, Kafka, and FFmpeg-backed replay fan-out."""

    def __init__(self, config: ReplayConfig) -> None:
        self.config = config
        self._producer: Any = None
        self._minio: Any = None
        self._frame_type: type[Any] | None = None
        self._temp_dir: tempfile.TemporaryDirectory[str] | None = None
        self._frame_cache: list[SourceFrames] = []
        self._shutdown = asyncio.Event()
        self.frames_uploaded = 0

    async def start(self) -> None:
        """Initialise dependencies and prepare source frame caches."""
        self._ensure_ffmpeg()
        self._frame_type = load_frame_ref_type()
        self._producer = self._build_kafka_producer()
        self._minio = await asyncio.to_thread(self._build_minio_client)
        self._temp_dir = tempfile.TemporaryDirectory(prefix="cilex-replay-")
        await self._ensure_bucket()
        self._frame_cache = await self._prepare_source_frames()
        if not self._frame_cache:
            raise RuntimeError(
                f"no replayable frames extracted from {self.config.source_dir}"
            )

    async def close(self) -> None:
        """Flush pending Kafka messages and clean up temp files."""
        self._shutdown.set()
        if self._producer is not None:
            await asyncio.to_thread(self._producer.flush, 10.0)
        if self._temp_dir is not None:
            self._temp_dir.cleanup()

    async def run(self) -> None:
        """Publish replayed frame traffic until the configured duration ends."""
        stop_event = asyncio.Event()
        _install_signal_handlers(stop_event)
        camera_tasks = [
            asyncio.create_task(
                self._run_camera(camera_id, self._frame_cache[index % len(self._frame_cache)], stop_event)
            )
            for index, camera_id in enumerate(self.config.camera_ids)
        ]

        try:
            if self.config.duration_s is None:
                await stop_event.wait()
            else:
                await asyncio.wait_for(stop_event.wait(), timeout=self.config.duration_s)
        except asyncio.TimeoutError:
            LOGGER.info("replay duration reached; stopping")
        finally:
            stop_event.set()
            await asyncio.gather(*camera_tasks, return_exceptions=True)

    async def _run_camera(
        self,
        camera_id: str,
        source_frames: SourceFrames,
        stop_event: asyncio.Event,
    ) -> None:
        if self._minio is None or self._producer is None or self._frame_type is None:
            raise RuntimeError("replay publisher not started")

        frame_index = 0
        frame_sequence = 0
        interval_s = 1.0 / float(self.config.fps)

        while not self._shutdown.is_set() and not stop_event.is_set():
            started_at = time.monotonic()
            frame_path = source_frames.frame_paths[frame_index % len(source_frames.frame_paths)]
            frame_index += 1
            frame_sequence += 1
            frame_bytes = await asyncio.to_thread(frame_path.read_bytes)
            await self._publish_frame(camera_id, frame_sequence, frame_bytes)

            remaining = interval_s - (time.monotonic() - started_at)
            if remaining > 0:
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=remaining)
                except asyncio.TimeoutError:
                    continue

    async def _publish_frame(
        self,
        camera_id: str,
        frame_sequence: int,
        frame_bytes: bytes,
    ) -> None:
        if self._minio is None or self._producer is None or self._frame_type is None:
            raise RuntimeError("replay publisher not started")

        capture_time = utc_now()
        source_capture_epoch = capture_time.timestamp()
        edge_receive_epoch = source_capture_epoch + 0.010
        core_ingest_epoch = source_capture_epoch + 0.020
        object_key = f"{camera_id}/{frame_sequence:08d}.jpg"

        await asyncio.to_thread(
            self._minio.put_object,
            self.config.source_bucket,
            object_key,
            io.BytesIO(frame_bytes),
            len(frame_bytes),
            content_type="image/jpeg",
        )

        message = self._frame_type()
        message.frame_id = str(uuid.uuid4())
        message.camera_id = camera_id
        message.frame_uri = f"s3://{self.config.source_bucket}/{object_key}"
        message.frame_sequence = frame_sequence
        message.width_px = self.config.frame_width_px
        message.height_px = self.config.frame_height_px
        message.codec = "jpeg"
        set_proto_timestamp(message.timestamps.source_capture_ts, source_capture_epoch)
        set_proto_timestamp(message.timestamps.edge_receive_ts, edge_receive_epoch)
        set_proto_timestamp(message.timestamps.core_ingest_ts, core_ingest_epoch)

        await asyncio.to_thread(self._produce_frame_ref, camera_id, message.SerializeToString())
        self.frames_uploaded += 1

    async def _prepare_source_frames(self) -> list[SourceFrames]:
        source_files = sorted(
            path
            for path in self.config.source_dir.rglob("*")
            if path.is_file() and path.suffix.lower() in SUPPORTED_VIDEO_SUFFIXES
        )
        if not source_files:
            raise RuntimeError(
                f"no video files found in {self.config.source_dir}; expected one of "
                f"{sorted(SUPPORTED_VIDEO_SUFFIXES)}"
            )

        prepared: list[SourceFrames] = []
        for index, source_path in enumerate(source_files):
            LOGGER.info("extracting replay frames from %s", source_path)
            frame_paths = await asyncio.to_thread(self._extract_frames_for_source, source_path, index)
            if not frame_paths:
                LOGGER.warning("skipping %s because FFmpeg produced no frames", source_path)
                continue
            prepared.append(SourceFrames(source_path=source_path, frame_paths=frame_paths))
        return prepared

    def _extract_frames_for_source(self, source_path: Path, index: int) -> list[Path]:
        if self._temp_dir is None:
            raise RuntimeError("temporary replay directory not initialised")
        output_dir = Path(self._temp_dir.name) / f"source-{index:02d}"
        output_dir.mkdir(parents=True, exist_ok=True)
        command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source_path),
        ]
        if self.config.max_source_seconds > 0:
            command.extend(["-t", str(self.config.max_source_seconds)])
        command.extend(
            [
                "-vf",
                (
                    f"fps={self.config.fps},"
                    f"scale={self.config.frame_width_px}:{self.config.frame_height_px}:flags=lanczos"
                ),
                "-q:v",
                "2",
                str(output_dir / "frame-%06d.jpg"),
            ]
        )

        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode != 0:
            message = completed.stderr.strip() or completed.stdout.strip()
            raise RuntimeError(
                f"ffmpeg failed for {source_path}: {message or 'unknown error'}"
            )
        return sorted(output_dir.glob("frame-*.jpg"))

    def _build_minio_client(self) -> Any:
        try:
            from minio import Minio  # noqa: PLC0415
        except ImportError as exc:
            raise RuntimeError(
                "missing optional dependency 'minio'; install it to upload replay frames"
            ) from exc
        return Minio(
            self.config.minio_url,
            access_key=self.config.minio_access_key,
            secret_key=self.config.minio_secret_key,
            secure=self.config.minio_secure,
        )

    def _build_kafka_producer(self) -> Any:
        try:
            from confluent_kafka import Producer  # noqa: PLC0415
        except ImportError as exc:
            raise RuntimeError(
                "missing optional dependency 'confluent-kafka'; install it to publish replay frames"
            ) from exc
        return Producer(
            {
                "bootstrap.servers": self.config.kafka_bootstrap,
                "acks": "all",
                "enable.idempotence": True,
                "compression.type": "zstd",
                **_security_protocol_config(self.config.kafka_security_protocol),
            }
        )

    async def _ensure_bucket(self) -> None:
        if self._minio is None:
            raise RuntimeError("MinIO client unavailable")
        exists = await asyncio.to_thread(self._minio.bucket_exists, self.config.source_bucket)
        if not exists:
            await asyncio.to_thread(self._minio.make_bucket, self.config.source_bucket)

    def _produce_frame_ref(self, camera_id: str, payload: bytes) -> None:
        if self._producer is None:
            raise RuntimeError("Kafka producer unavailable")
        try:
            self._producer.produce(
                self.config.kafka_topic,
                key=camera_id.encode("utf-8"),
                value=payload,
                headers=[("x-proto-schema", FRAME_PROTO_SCHEMA)],
            )
            self._producer.poll(0)
        except BufferError:
            self._producer.flush(1.0)
            self._producer.produce(
                self.config.kafka_topic,
                key=camera_id.encode("utf-8"),
                value=payload,
                headers=[("x-proto-schema", FRAME_PROTO_SCHEMA)],
            )
            self._producer.poll(0)

    def _ensure_ffmpeg(self) -> None:
        if shutil.which("ffmpeg") is None:
            raise RuntimeError("ffmpeg is required for replay_streams.py")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--cameras", type=int, default=100)
    parser.add_argument("--fps", type=int, default=10)
    parser.add_argument(
        "--duration",
        type=int,
        default=14_400,
        help="Runtime in seconds. Use 0 for an indefinite run.",
    )
    parser.add_argument("--minio-url", default="localhost:9000")
    parser.add_argument("--minio-access-key", default="minioadmin")
    parser.add_argument("--minio-secret-key", default="minioadmin123")
    parser.add_argument("--minio-secure", action="store_true")
    parser.add_argument("--kafka-bootstrap", default="localhost:19092")
    parser.add_argument("--kafka-topic", default="frames.sampled.refs")
    parser.add_argument("--kafka-security-protocol", default="PLAINTEXT")
    parser.add_argument("--site-id", default="scale-test-site")
    parser.add_argument("--source-bucket", default="frame-blobs")
    parser.add_argument("--camera-prefix", default="cam")
    parser.add_argument("--frame-width", type=int, default=1280)
    parser.add_argument("--frame-height", type=int, default=720)
    parser.add_argument(
        "--max-source-seconds",
        type=int,
        default=300,
        help="Maximum seconds extracted from each source video before cycling.",
    )
    parser.add_argument("--log-level", default="INFO")
    return parser.parse_args()


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


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


def _security_protocol_config(protocol: str) -> dict[str, str]:
    if protocol == "PLAINTEXT":
        return {}
    return {"security.protocol": protocol}


def _build_config(args: argparse.Namespace) -> ReplayConfig:
    duration_s = None if args.duration == 0 else args.duration
    return ReplayConfig(
        source_dir=args.source_dir,
        camera_count=args.cameras,
        fps=args.fps,
        duration_s=duration_s,
        minio_url=args.minio_url,
        minio_access_key=args.minio_access_key,
        minio_secret_key=args.minio_secret_key,
        minio_secure=args.minio_secure,
        kafka_bootstrap=args.kafka_bootstrap,
        kafka_topic=args.kafka_topic,
        kafka_security_protocol=args.kafka_security_protocol,
        site_id=args.site_id,
        source_bucket=args.source_bucket,
        camera_prefix=args.camera_prefix,
        frame_width_px=args.frame_width,
        frame_height_px=args.frame_height,
        max_source_seconds=args.max_source_seconds,
    )


async def _async_main(config: ReplayConfig) -> None:
    publisher = ReplayPublisher(config)
    await publisher.start()
    try:
        await publisher.run()
    finally:
        await publisher.close()
    LOGGER.info(
        "replay finished: cameras=%d fps=%d frames_uploaded=%d",
        config.camera_count,
        config.fps,
        publisher.frames_uploaded,
    )


def main() -> None:
    args = parse_args()
    setup_logging(args.log_level)
    config = _build_config(args)
    asyncio.run(_async_main(config))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # pragma: no cover
        raise SystemExit(str(exc)) from exc
