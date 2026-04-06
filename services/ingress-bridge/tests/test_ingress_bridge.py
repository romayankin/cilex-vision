"""Ingress Bridge unit tests."""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from pathlib import Path

import pytest

import main
from config import Settings, SiteConfig


class FakeTimestamp:
    """Tiny protobuf-like timestamp stub."""

    def __init__(self, seconds: int = 0, nanos: int = 0) -> None:
        self.seconds = seconds
        self.nanos = nanos


class FakeVideoTimestamp:
    """Tiny protobuf-like VideoTimestamp stub."""

    def __init__(
        self,
        source_capture_ts: FakeTimestamp | None = None,
        edge_receive_ts: FakeTimestamp | None = None,
        core_ingest_ts: FakeTimestamp | None = None,
    ) -> None:
        self.source_capture_ts = source_capture_ts or FakeTimestamp()
        self.edge_receive_ts = edge_receive_ts or FakeTimestamp()
        self.core_ingest_ts = core_ingest_ts or FakeTimestamp()


class FakeFrameRef:
    """Small JSON-backed stand-in for the generated FrameRef class."""

    def __init__(self) -> None:
        self.frame_id = ""
        self.camera_id = ""
        self.frame_uri = ""
        self.frame_sequence = 0
        self.timestamps = FakeVideoTimestamp()
        self.width_px = 0
        self.height_px = 0
        self.codec = ""

    def ParseFromString(self, payload: bytes) -> None:
        data = json.loads(payload.decode("utf-8"))
        self.frame_id = data["frame_id"]
        self.camera_id = data["camera_id"]
        self.frame_uri = data["frame_uri"]
        self.frame_sequence = int(data["frame_sequence"])
        ts = data["timestamps"]
        self.timestamps = FakeVideoTimestamp(
            source_capture_ts=FakeTimestamp(**ts["source_capture_ts"]),
            edge_receive_ts=FakeTimestamp(**ts["edge_receive_ts"]),
            core_ingest_ts=FakeTimestamp(**ts.get("core_ingest_ts", {"seconds": 0, "nanos": 0})),
        )
        self.width_px = int(data.get("width_px", 0))
        self.height_px = int(data.get("height_px", 0))
        self.codec = str(data.get("codec", "jpeg"))

    def SerializeToString(self) -> bytes:
        payload = {
            "frame_id": self.frame_id,
            "camera_id": self.camera_id,
            "frame_uri": self.frame_uri,
            "frame_sequence": self.frame_sequence,
            "timestamps": {
                "source_capture_ts": {"seconds": self.timestamps.source_capture_ts.seconds, "nanos": self.timestamps.source_capture_ts.nanos},
                "edge_receive_ts": {"seconds": self.timestamps.edge_receive_ts.seconds, "nanos": self.timestamps.edge_receive_ts.nanos},
                "core_ingest_ts": {"seconds": self.timestamps.core_ingest_ts.seconds, "nanos": self.timestamps.core_ingest_ts.nanos},
            },
            "width_px": self.width_px,
            "height_px": self.height_px,
            "codec": self.codec,
        }
        return json.dumps(payload, sort_keys=True).encode("utf-8")


class FakeSchemaValidator:
    """Schema validator that parses the fake JSON payload."""

    def __init__(self) -> None:
        self.seen_payloads: list[bytes] = []

    def validate_frame(self, payload: bytes) -> FakeFrameRef:
        self.seen_payloads.append(payload)
        frame = FakeFrameRef()
        frame.ParseFromString(payload)
        if not frame.frame_uri.startswith(("s3://", "minio://")):
            raise main.SchemaValidationError("invalid_frame_uri")
        if main.timestamp_to_epoch_us(frame.timestamps.edge_receive_ts) <= 0:
            raise main.SchemaValidationError("missing_edge_receive_ts")
        if main.timestamp_to_epoch_us(frame.timestamps.source_capture_ts) <= 0:
            raise main.SchemaValidationError("missing_source_capture_ts")
        return frame


class FakeBlobOffloader:
    """Captures auxiliary blob uploads in memory."""

    def __init__(self) -> None:
        self.uploads: list[tuple[str, str, str, bytes]] = []

    async def offload(self, site_id: str, camera_id: str, name: str, data: bytes) -> str:
        self.uploads.append((site_id, camera_id, name, data))
        return f"minio://frame-blobs/{site_id}/{camera_id}/{name}"

    async def is_reachable(self) -> bool:
        return True


class FakeKafkaProducer:
    """Configurable Kafka producer fake."""

    def __init__(self) -> None:
        self.records: list[main.PreparedKafkaRecord] = []
        self.failures_remaining = 0

    async def produce(self, record: main.PreparedKafkaRecord) -> None:
        if self.failures_remaining > 0:
            self.failures_remaining -= 1
            raise RuntimeError("mock producer failure")
        self.records.append(record)

    async def is_ready(self) -> bool:
        return True


@dataclass
class FakeMetadata:
    """JetStream delivery metadata stub."""

    num_delivered: int = 1


@dataclass
class FakeNatsMessage:
    """Minimal NATS message stub for unit tests."""

    subject: str
    data: bytes
    headers: dict[str, str] | None = None
    metadata: FakeMetadata | None = None
    acked: bool = False
    naked: bool = False

    async def ack(self) -> None:
        self.acked = True

    async def nak(self) -> None:
        self.naked = True


class FakeNatsAdapter:
    """Small in-memory NATS adapter for service tests."""

    def __init__(self, messages: list[FakeNatsMessage] | None = None) -> None:
        self.messages = list(messages or [])
        self.dlq: list[tuple[main.PendingBridgeMessage, str]] = []
        self.ensure_connected_calls = 0
        self.closed = False
        self._connected = True

    @property
    def is_connected(self) -> bool:
        return self._connected and not self.closed

    async def ensure_connected(self) -> None:
        self.ensure_connected_calls += 1

    async def fetch_batch(self) -> list[FakeNatsMessage]:
        batch = list(self.messages)
        self.messages.clear()
        return batch

    async def publish_dlq(self, pending: main.PendingBridgeMessage, reason: str) -> None:
        self.dlq.append((pending, reason))

    async def close(self) -> None:
        self.closed = True
        self._connected = False


def build_settings(tmp_path: Path) -> Settings:
    """Create test settings with a temp spool path."""
    return Settings(
        spool={
            "path": str(tmp_path / "spool"),
            "max_bytes": 1024 * 1024,
            "resume_pct": 80,
            "replay_rate_limit_msg_per_sec": 1000,
            "spool_drain_rate_limit_msg_per_sec": 1000,
        },
        sites=[SiteConfig(site_id="site-a", rate_limit_msg_per_sec=500)],
    )


def encode_frame_payload(
    *,
    camera_id: str = "cam-01",
    frame_sequence: int = 7,
    source_seconds: int = 1_700_000_000,
    edge_seconds: int = 1_700_000_001,
) -> bytes:
    """Build a fake FrameRef payload."""
    payload = {
        "frame_id": "frame-123",
        "camera_id": camera_id,
        "frame_uri": "minio://frame-blobs/site-a/cam-01/frame.jpg",
        "frame_sequence": frame_sequence,
        "timestamps": {
            "source_capture_ts": {"seconds": source_seconds, "nanos": 250_000_000},
            "edge_receive_ts": {"seconds": edge_seconds, "nanos": 0},
            "core_ingest_ts": {"seconds": 0, "nanos": 0},
        },
        "width_px": 1280,
        "height_px": 720,
        "codec": "jpeg",
    }
    return json.dumps(payload).encode("utf-8")


@pytest.fixture(autouse=True)
def fake_frame_type(monkeypatch) -> None:
    """Keep tests independent from generated protobuf code."""
    monkeypatch.setattr(main, "load_frame_ref_type", lambda: FakeFrameRef)


@pytest.mark.asyncio
async def test_prepare_record_builds_exact_idempotent_key(tmp_path: Path) -> None:
    settings = build_settings(tmp_path)
    service = main.IngressBridgeService(
        settings,
        schema_validator=FakeSchemaValidator(),
        blob_offloader=FakeBlobOffloader(),
        kafka_producer=FakeKafkaProducer(),
        nats_adapter=FakeNatsAdapter(),
        clock=lambda: 1_700_000_010.5,
    )

    pending = main.PendingBridgeMessage(
        subject="frames.live.site-a.cam-01",
        payload=encode_frame_payload(),
        headers={},
        site_id="site-a",
        lane="live",
    )
    record = await service.prepare_record(pending)

    assert record.key == "site-a:cam-01:1700000000250000:7"
    stamped = FakeFrameRef()
    stamped.ParseFromString(record.payload)
    assert stamped.timestamps.core_ingest_ts.seconds == 1_700_000_010
    assert stamped.timestamps.edge_receive_ts.seconds == 1_700_000_001


@pytest.mark.asyncio
async def test_large_auxiliary_blob_is_offloaded_before_kafka(tmp_path: Path) -> None:
    settings = build_settings(tmp_path)
    blob_offloader = FakeBlobOffloader()
    service = main.IngressBridgeService(
        settings,
        schema_validator=FakeSchemaValidator(),
        blob_offloader=blob_offloader,
        kafka_producer=FakeKafkaProducer(),
        nats_adapter=FakeNatsAdapter(),
        clock=lambda: 1_700_000_020.0,
    )

    aux_blob = base64.b64encode(b"x" * (main.OFFLOAD_THRESHOLD_BYTES + 1)).decode("utf-8")
    pending = main.PendingBridgeMessage(
        subject="frames.live.site-a.cam-01",
        payload=encode_frame_payload(),
        headers={"X-Aux-Blob-debug": aux_blob},
        site_id="site-a",
        lane="live",
    )
    record = await service.prepare_record(pending)

    assert blob_offloader.uploads
    assert record.headers["x-blob-ref-debug"].startswith("minio://frame-blobs/")


@pytest.mark.asyncio
async def test_kafka_failure_spools_then_recovery_drains(tmp_path: Path) -> None:
    settings = build_settings(tmp_path)
    producer = FakeKafkaProducer()
    producer.failures_remaining = 1
    service = main.IngressBridgeService(
        settings,
        schema_validator=FakeSchemaValidator(),
        blob_offloader=FakeBlobOffloader(),
        kafka_producer=producer,
        nats_adapter=FakeNatsAdapter(),
        clock=lambda: 1_700_000_030.0,
    )
    msg = FakeNatsMessage(
        subject="frames.live.site-a.cam-01",
        data=encode_frame_payload(frame_sequence=11),
        headers={},
    )

    outcome = await service.handle_message(msg, delivery_attempt=1)
    assert outcome == "spooled_prepared"
    assert msg.acked is True
    assert service.spool.message_count == 1
    assert not producer.records

    drained = await service.drain_once()
    assert drained == 1
    assert service.spool.message_count == 0
    assert len(producer.records) == 1
    assert producer.records[0].key == "site-a:cam-01:1700000000250000:11"


@pytest.mark.asyncio
async def test_schema_failure_nacks_before_dlq_threshold(tmp_path: Path) -> None:
    settings = build_settings(tmp_path)

    class RejectingValidator(FakeSchemaValidator):
        def validate_frame(self, payload: bytes) -> FakeFrameRef:
            raise main.SchemaValidationError("broken_payload")

    service = main.IngressBridgeService(
        settings,
        schema_validator=RejectingValidator(),
        blob_offloader=FakeBlobOffloader(),
        kafka_producer=FakeKafkaProducer(),
        nats_adapter=FakeNatsAdapter(),
    )
    msg = FakeNatsMessage(
        subject="frames.live.site-a.cam-01",
        data=encode_frame_payload(),
    )

    outcome = await service.handle_message(msg, delivery_attempt=1)
    assert outcome == "nack"
    assert msg.naked is True
    assert msg.acked is False


@pytest.mark.asyncio
async def test_schema_failure_routes_to_dlq_after_max_redeliver(tmp_path: Path) -> None:
    settings = build_settings(tmp_path)
    adapter = FakeNatsAdapter()

    class RejectingValidator(FakeSchemaValidator):
        def validate_frame(self, payload: bytes) -> FakeFrameRef:
            raise main.SchemaValidationError("broken_payload")

    service = main.IngressBridgeService(
        settings,
        schema_validator=RejectingValidator(),
        blob_offloader=FakeBlobOffloader(),
        kafka_producer=FakeKafkaProducer(),
        nats_adapter=adapter,
    )
    msg = FakeNatsMessage(
        subject="frames.live.site-a.cam-01",
        data=encode_frame_payload(),
        metadata=FakeMetadata(num_delivered=settings.nats.max_redeliver),
    )

    outcome = await service.handle_message(msg, delivery_attempt=msg.metadata.num_delivered)
    assert outcome == "dlq"
    assert msg.acked is True
    assert msg.naked is False
    assert adapter.dlq
    assert adapter.dlq[0][1] == "broken_payload"


@pytest.mark.asyncio
async def test_consume_once_fetches_from_nats_adapter(tmp_path: Path) -> None:
    settings = build_settings(tmp_path)
    producer = FakeKafkaProducer()
    message = FakeNatsMessage(
        subject="frames.live.site-a.cam-01",
        data=encode_frame_payload(frame_sequence=19),
        metadata=FakeMetadata(num_delivered=2),
    )
    adapter = FakeNatsAdapter(messages=[message])
    service = main.IngressBridgeService(
        settings,
        schema_validator=FakeSchemaValidator(),
        blob_offloader=FakeBlobOffloader(),
        kafka_producer=producer,
        nats_adapter=adapter,
        clock=lambda: 1_700_000_040.0,
    )

    processed = await service.consume_once()

    assert processed == 1
    assert adapter.ensure_connected_calls == 1
    assert message.acked is True
    assert service.nats_connected is True
    assert producer.records[0].key == "site-a:cam-01:1700000000250000:19"
