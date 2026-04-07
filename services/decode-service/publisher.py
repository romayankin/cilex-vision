"""Kafka publisher for decoded FrameRef messages.

Publishes to ``frames.decoded.refs`` with the same FrameRef protobuf
schema as the input, but with an updated ``frame_uri`` pointing to the
decoded (RGB, resized) frame in the ``decoded-frames`` MinIO bucket.
"""

from __future__ import annotations

import logging
import ssl
import sys
from pathlib import Path
from typing import Any

from config import KafkaConfig
from metrics import PUBLISH_ERRORS

logger = logging.getLogger(__name__)

PROTO_PATH = Path(__file__).resolve().parent / "proto_gen"
if str(PROTO_PATH) not in sys.path:
    sys.path.insert(0, str(PROTO_PATH))


def _load_frame_ref_type() -> type[Any]:
    from vidanalytics.v1.frame import frame_pb2  # noqa: PLC0415

    return frame_pb2.FrameRef


def _set_timestamp(ts_field: Any, epoch_s: float) -> None:
    """Set a google.protobuf.Timestamp from epoch seconds."""
    ts_field.seconds = int(epoch_s)
    ts_field.nanos = int((epoch_s - int(epoch_s)) * 1_000_000_000)


def build_frame_ref_proto(
    frame_id: str,
    camera_id: str,
    frame_uri: str,
    frame_sequence: int,
    width: int,
    height: int,
    codec: str,
    source_capture_ts: float,
    edge_receive_ts: float,
    core_ingest_ts: float,
) -> bytes:
    """Serialise a FrameRef protobuf for the decoded frame."""
    FrameRef = _load_frame_ref_type()

    msg = FrameRef()
    msg.frame_id = frame_id
    msg.camera_id = camera_id
    msg.frame_uri = frame_uri
    msg.frame_sequence = frame_sequence
    msg.width_px = width
    msg.height_px = height
    msg.codec = codec

    _set_timestamp(msg.timestamps.source_capture_ts, source_capture_ts)
    _set_timestamp(msg.timestamps.edge_receive_ts, edge_receive_ts)
    _set_timestamp(msg.timestamps.core_ingest_ts, core_ingest_ts)

    return msg.SerializeToString()


class KafkaPublisher:
    """Async Kafka producer for decoded FrameRef messages."""

    def __init__(self, cfg: KafkaConfig) -> None:
        self._cfg = cfg
        self._producer = None
        self._connected = False

    async def connect(self) -> None:
        """Create and start the aiokafka producer."""
        try:
            from aiokafka import AIOKafkaProducer  # noqa: PLC0415
        except ImportError as exc:
            raise RuntimeError(
                "missing optional dependency 'aiokafka'; install requirements.txt"
            ) from exc

        ssl_context = self._build_ssl_context()
        self._producer = AIOKafkaProducer(
            bootstrap_servers=self._cfg.bootstrap_servers,
            security_protocol=self._cfg.security_protocol,
            sasl_mechanism=self._cfg.sasl_mechanism,
            sasl_plain_username=self._cfg.sasl_username,
            sasl_plain_password=self._cfg.sasl_password,
            ssl_context=ssl_context,
            acks="all",
            enable_idempotence=True,
            compression_type="zstd",
        )
        await self._producer.start()
        self._connected = True

    async def close(self) -> None:
        if self._producer is not None:
            await self._producer.stop()
            self._connected = False

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def publish_frame_ref(
        self,
        frame_id: str,
        camera_id: str,
        frame_uri: str,
        frame_sequence: int,
        width: int,
        height: int,
        codec: str,
        source_capture_ts: float,
        edge_receive_ts: float,
        core_ingest_ts: float,
    ) -> None:
        """Publish a decoded FrameRef to the output topic."""
        payload = build_frame_ref_proto(
            frame_id=frame_id,
            camera_id=camera_id,
            frame_uri=frame_uri,
            frame_sequence=frame_sequence,
            width=width,
            height=height,
            codec=codec,
            source_capture_ts=source_capture_ts,
            edge_receive_ts=edge_receive_ts,
            core_ingest_ts=core_ingest_ts,
        )

        headers = [
            ("x-proto-schema", b"vidanalytics.v1.frame.FrameRef"),
        ]

        await self._send(
            self._cfg.output_topic,
            key=camera_id.encode(),
            value=payload,
            headers=headers,
        )

    async def _send(
        self,
        topic: str,
        key: bytes,
        value: bytes,
        headers: list[tuple[str, bytes]],
    ) -> None:
        if self._producer is None:
            PUBLISH_ERRORS.inc()
            return
        try:
            await self._producer.send(
                topic,
                key=key,
                value=value,
                headers=headers,
            )
        except Exception:
            PUBLISH_ERRORS.inc()
            logger.warning("Failed to publish to %s", topic, exc_info=True)

    def _build_ssl_context(self) -> ssl.SSLContext | None:
        if not any([
            self._cfg.ssl_ca_file,
            self._cfg.ssl_cert_file,
            self._cfg.ssl_key_file,
        ]):
            return None
        ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
        if self._cfg.ssl_ca_file:
            ctx.load_verify_locations(self._cfg.ssl_ca_file)
        if self._cfg.ssl_cert_file and self._cfg.ssl_key_file:
            ctx.load_cert_chain(self._cfg.ssl_cert_file, self._cfg.ssl_key_file)
        return ctx
