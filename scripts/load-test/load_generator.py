#!/usr/bin/env python3
"""Synthetic load generation for the end-to-end stress-test harness."""

from __future__ import annotations

import asyncio
import io
import logging
import sys
import time
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from common import build_query_headers, http_get_json, isoformat_utc, load_frame_ref_type, set_proto_timestamp, utc_now  # noqa: E402
from models import TestConfig  # noqa: E402


LOGGER = logging.getLogger("load_generator")
FRAME_PROTO_SCHEMA = b"vidanalytics.v1.frame.FrameRef"
ACTIVE_DUTY_CYCLE = 0.15


class LoadGenerator:
    """Generate synthetic frame and query traffic against a live deployment."""

    def __init__(self, config: TestConfig) -> None:
        self.config = config
        self._producer: Any = None
        self._minio: Any = None
        self._frame_type: type[Any] | None = None
        self._replay_frames: list[Path] = []
        self._camera_static_frame: dict[str, bytes] = {}
        self._camera_replay_index: dict[str, int] = {}
        self._shutdown = asyncio.Event()
        self._query_headers = build_query_headers(
            secret=config.query_jwt_secret,
            cookie_name=config.query_cookie_name,
            role=config.query_role,
            camera_scope=None if config.query_role == "admin" else config.camera_ids,
        )
        self.frames_published = 0
        self.queries_sent = 0
        self.query_failures = 0

    async def start(self) -> None:
        """Connect to Kafka and MinIO, and prepare frame sources."""
        try:
            from confluent_kafka import Producer  # noqa: PLC0415
        except ImportError as exc:
            raise RuntimeError(
                "missing optional dependency 'confluent-kafka'; install service requirements"
            ) from exc

        self._frame_type = load_frame_ref_type()
        self._producer = Producer(
            {
                "bootstrap.servers": self.config.kafka_bootstrap,
                "acks": "all",
                "enable.idempotence": True,
                "compression.type": "zstd",
                **_security_protocol_config(self.config.kafka_security_protocol),
            }
        )
        self._minio = await asyncio.to_thread(self._create_minio_client)
        await self._ensure_source_bucket()

        if self.config.replay_frame_dir is not None:
            self._replay_frames = sorted(
                path
                for path in self.config.replay_frame_dir.iterdir()
                if path.suffix.lower() in {".jpg", ".jpeg", ".png"}
            )
            if not self._replay_frames:
                raise RuntimeError(
                    f"no replay images found in {self.config.replay_frame_dir}"
                )

    async def close(self) -> None:
        """Flush pending Kafka messages and stop background activity."""
        self._shutdown.set()
        if self._producer is not None:
            await asyncio.to_thread(self._producer.flush, 5.0)

    async def generate_camera_load(
        self,
        camera_id: str,
        fps: int = 5,
        *,
        stop_event: asyncio.Event | None = None,
    ) -> None:
        """Publish synthetic ``FrameRef`` traffic for one camera."""
        if fps <= 0:
            raise ValueError("fps must be > 0")
        interval_s = 1.0 / fps
        frame_sequence = 0
        local_stop = stop_event or asyncio.Event()
        while not self._shutdown.is_set() and not local_stop.is_set():
            started = time.monotonic()
            frame_sequence += 1
            await self._publish_frame(camera_id, frame_sequence)
            remaining = interval_s - (time.monotonic() - started)
            if remaining > 0:
                try:
                    await asyncio.wait_for(local_stop.wait(), timeout=remaining)
                except asyncio.TimeoutError:
                    continue

    async def generate_query_load(
        self,
        qps: int = 10,
        *,
        stop_event: asyncio.Event | None = None,
    ) -> None:
        """Generate authenticated query load against the Query API."""
        if qps <= 0:
            raise ValueError("qps must be > 0")
        interval_s = 1.0 / qps
        local_stop = stop_event or asyncio.Event()
        endpoints = ("/detections", "/tracks", "/events")
        request_index = 0

        while not self._shutdown.is_set() and not local_stop.is_set():
            started = time.monotonic()
            endpoint = endpoints[request_index % len(endpoints)]
            camera_id = self.config.camera_ids[request_index % len(self.config.camera_ids)]
            params = self._build_query_params(camera_id)
            try:
                await asyncio.to_thread(
                    http_get_json,
                    f"{self.config.query_api_url.rstrip('/')}{endpoint}",
                    params=params,
                    headers=self._query_headers,
                )
                self.queries_sent += 1
            except Exception:
                self.query_failures += 1
                LOGGER.warning("query load request failed for %s", endpoint, exc_info=True)

            request_index += 1
            remaining = interval_s - (time.monotonic() - started)
            if remaining > 0:
                try:
                    await asyncio.wait_for(local_stop.wait(), timeout=remaining)
                except asyncio.TimeoutError:
                    continue

    async def _publish_frame(self, camera_id: str, frame_sequence: int) -> None:
        if self._producer is None or self._minio is None or self._frame_type is None:
            raise RuntimeError("load generator not started")

        capture_time = utc_now()
        source_capture_ts = capture_time.timestamp()
        edge_receive_ts = source_capture_ts + 0.010
        core_ingest_ts = source_capture_ts + 0.020

        frame_bytes = await asyncio.to_thread(
            self._render_frame_bytes,
            camera_id,
            frame_sequence,
        )
        date_prefix = capture_time.strftime("%Y-%m-%d")
        object_key = (
            f"{camera_id}/{date_prefix}/"
            f"{int(source_capture_ts * 1000)}-{frame_sequence:08d}.jpg"
        )
        await asyncio.to_thread(
            self._minio.put_object,
            self.config.source_bucket,
            object_key,
            io.BytesIO(frame_bytes),
            len(frame_bytes),
            "image/jpeg",
        )
        frame_uri = f"s3://{self.config.source_bucket}/{object_key}"

        message = self._frame_type()
        message.frame_id = str(uuid.uuid4())
        message.camera_id = camera_id
        message.frame_uri = frame_uri
        message.frame_sequence = frame_sequence
        message.width_px = self.config.source_width_px
        message.height_px = self.config.source_height_px
        message.codec = "jpeg"
        set_proto_timestamp(message.timestamps.source_capture_ts, source_capture_ts)
        set_proto_timestamp(message.timestamps.edge_receive_ts, edge_receive_ts)
        set_proto_timestamp(message.timestamps.core_ingest_ts, core_ingest_ts)

        payload = message.SerializeToString()
        await asyncio.to_thread(
            self._safe_produce,
            camera_id,
            payload,
        )
        self.frames_published += 1

    def _build_query_params(self, camera_id: str) -> dict[str, str]:
        end_time = utc_now()
        start_time = end_time - timedelta(minutes=10)
        return {
            "camera_id": camera_id,
            "start": isoformat_utc(start_time),
            "end": isoformat_utc(end_time),
            "limit": "25",
            "offset": "0",
        }

    def _render_frame_bytes(self, camera_id: str, frame_sequence: int) -> bytes:
        if self._replay_frames:
            return self._render_replay_frame(camera_id, frame_sequence)
        return self._render_synthetic_frame(camera_id, frame_sequence)

    def _render_replay_frame(self, camera_id: str, frame_sequence: int) -> bytes:
        try:
            from PIL import Image  # noqa: PLC0415
        except ImportError as exc:
            raise RuntimeError(
                "missing optional dependency 'Pillow'; needed for replay frame resize"
            ) from exc

        index = self._camera_replay_index.get(camera_id, 0)
        source_path = self._replay_frames[index % len(self._replay_frames)]
        self._camera_replay_index[camera_id] = index + 1
        with Image.open(source_path) as image:
            frame = image.convert("RGB").resize(
                (self.config.source_width_px, self.config.source_height_px)
            )
            buffer = io.BytesIO()
            frame.save(buffer, format="JPEG", quality=90)
            return buffer.getvalue()

    def _render_synthetic_frame(self, camera_id: str, frame_sequence: int) -> bytes:
        try:
            from PIL import Image, ImageDraw  # noqa: PLC0415
        except ImportError as exc:
            raise RuntimeError(
                "missing optional dependency 'Pillow'; needed for synthetic frame generation"
            ) from exc

        is_active = _is_active_frame(frame_sequence)
        if not is_active and camera_id in self._camera_static_frame:
            return self._camera_static_frame[camera_id]

        image = Image.new(
            "RGB",
            (self.config.source_width_px, self.config.source_height_px),
            color=(42, 48, 58),
        )
        draw = ImageDraw.Draw(image)

        lane_top = int(self.config.source_height_px * 0.55)
        lane_bottom = int(self.config.source_height_px * 0.88)
        draw.rectangle(
            [(0, lane_top), (self.config.source_width_px, lane_bottom)],
            fill=(68, 74, 86),
        )

        draw.text((24, 20), f"{camera_id} seq={frame_sequence}", fill=(230, 230, 235))

        if is_active:
            x_offset = 120 + ((frame_sequence * 37) % max(self.config.source_width_px - 320, 1))
            car_top = int(self.config.source_height_px * 0.60)
            car_bottom = int(self.config.source_height_px * 0.74)
            draw.rounded_rectangle(
                [(x_offset, car_top), (x_offset + 220, car_bottom)],
                radius=24,
                fill=(214, 68, 68),
            )
            draw.ellipse(
                [(x_offset + 24, car_bottom - 8), (x_offset + 72, car_bottom + 40)],
                fill=(30, 30, 35),
            )
            draw.ellipse(
                [(x_offset + 148, car_bottom - 8), (x_offset + 196, car_bottom + 40)],
                fill=(30, 30, 35),
            )
        else:
            draw.rectangle(
                [(90, lane_top - 90), (170, lane_top + 80)],
                fill=(52, 110, 180),
            )
            draw.rectangle(
                [(1020, lane_top - 40), (1080, lane_top + 70)],
                fill=(112, 170, 76),
            )

        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=88)
        rendered = buffer.getvalue()
        if not is_active:
            self._camera_static_frame[camera_id] = rendered
        return rendered

    async def _ensure_source_bucket(self) -> None:
        if self._minio is None:
            raise RuntimeError("MinIO client unavailable")
        exists = await asyncio.to_thread(
            self._minio.bucket_exists,
            self.config.source_bucket,
        )
        if not exists:
            await asyncio.to_thread(
                self._minio.make_bucket,
                self.config.source_bucket,
            )

    def _create_minio_client(self) -> Any:
        try:
            from minio import Minio  # noqa: PLC0415
        except ImportError as exc:
            raise RuntimeError(
                "missing optional dependency 'minio'; install service requirements"
            ) from exc
        return Minio(
            self.config.minio_url,
            access_key=self.config.minio_access_key,
            secret_key=self.config.minio_secret_key,
            secure=self.config.minio_secure,
        )

    def _safe_produce(self, key: str, payload: bytes) -> None:
        if self._producer is None:
            raise RuntimeError("Kafka producer unavailable")
        try:
            self._producer.produce(
                self.config.kafka_frame_topic,
                key=key.encode("utf-8"),
                value=payload,
                headers=[("x-proto-schema", FRAME_PROTO_SCHEMA)],
            )
            self._producer.poll(0)
        except BufferError:
            self._producer.flush(1.0)
            self._producer.produce(
                self.config.kafka_frame_topic,
                key=key.encode("utf-8"),
                value=payload,
                headers=[("x-proto-schema", FRAME_PROTO_SCHEMA)],
            )
            self._producer.poll(0)


def _is_active_frame(frame_sequence: int) -> bool:
    cycle_length = 100
    active_frames = int(cycle_length * ACTIVE_DUTY_CYCLE)
    return (frame_sequence - 1) % cycle_length < active_frames


def _security_protocol_config(protocol: str) -> dict[str, str]:
    if protocol == "PLAINTEXT":
        return {}
    return {"security.protocol": protocol}


if __name__ == "__main__":
    raise SystemExit(
        "load_generator.py is a library module. Run run_stress_test.py instead."
    )
