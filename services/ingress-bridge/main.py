"""Ingress Bridge service.

Consumes FrameRef messages from NATS JetStream, validates them against the
registry-managed schema contract, stamps `core_ingest_ts`, and publishes to
Kafka with a deterministic idempotent key. Kafka or MinIO failures spill to a
local NVMe spool for later drain.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import io
import json
import logging
import os
import ssl
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from dataclasses import dataclass
from hashlib import md5, sha256
from pathlib import Path
from typing import Any, Callable, Protocol

from config import Settings, SiteConfig
from metrics import (
    BLOB_OFFLOAD,
    BLOB_OFFLOAD_LATENCY,
    CLOCK_DRIFT,
    DLQ_PUBLISHED,
    KAFKA_INFLIGHT,
    KAFKA_PRODUCE_LATENCY,
    MESSAGES_PRODUCED,
    MESSAGES_RECEIVED,
    MESSAGES_SPOOLED,
    NATS_CONSUMER_LAG,
    NATS_TO_KAFKA_LATENCY,
    RATE_LIMITED,
    RATE_LIMIT_HEADROOM,
    SCHEMA_REJECTION,
    SPOOL_CORRUPT,
    SPOOL_DEPTH_BYTES,
    SPOOL_DEPTH_MESSAGES,
    SPOOL_DRAINED,
    SPOOL_FILL_PCT,
    SPOOL_FULL,
)

logger = logging.getLogger(__name__)

PROTO_PATH = Path(__file__).resolve().parent / "proto_gen"
if str(PROTO_PATH) not in sys.path:
    sys.path.insert(0, str(PROTO_PATH))

AUXILIARY_HEADER_PREFIX = "X-Aux-Blob-"
OFFLOAD_THRESHOLD_BYTES = 100 * 1024
DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent / "config.yaml"
SPOOL_HEADER = struct.Struct(">II")


class SchemaValidationError(RuntimeError):
    """Raised when a NATS payload cannot be validated as a FrameRef."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class SpoolCorruptionError(RuntimeError):
    """Raised when a spool file cannot be decoded."""


class NatsMessageProtocol(Protocol):
    """Subset of the NATS message API used by the bridge."""

    subject: str
    data: bytes
    headers: dict[str, str] | None

    async def ack(self) -> None:
        """Acknowledge the message."""

    async def nak(self) -> None:
        """Negative-acknowledge the message for redelivery."""


def load_frame_ref_type() -> type[Any]:
    """Import the generated FrameRef type lazily."""
    try:
        from vidanalytics.v1.frame import frame_pb2  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - depends on local env
        raise RuntimeError(
            "generated protobufs not found; run `bash services/ingress-bridge/gen_proto.sh`"
        ) from exc
    return frame_pb2.FrameRef


def parse_args() -> argparse.Namespace:
    """CLI options for standalone service startup."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help="YAML config path.",
    )
    return parser.parse_args()


def setup_logging(level: str) -> None:
    """Configure stdlib logging."""
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def timestamp_to_epoch_us(ts: Any) -> int:
    """Convert a protobuf-like Timestamp to integer microseconds."""
    seconds = int(getattr(ts, "seconds", 0))
    nanos = int(getattr(ts, "nanos", 0))
    return seconds * 1_000_000 + nanos // 1_000


def timestamp_is_set(ts: Any) -> bool:
    """Return True when the protobuf-like Timestamp is non-zero."""
    return timestamp_to_epoch_us(ts) > 0


def set_timestamp_from_epoch(target: Any, epoch_seconds: float) -> None:
    """Populate a protobuf-like Timestamp in-place."""
    whole = int(epoch_seconds)
    nanos = int((epoch_seconds - whole) * 1_000_000_000)
    target.seconds = whole
    target.nanos = nanos


def parse_subject(subject: str) -> tuple[str, str, str]:
    """Extract lane, site_id, and camera_id from the NATS subject."""
    parts = subject.split(".")
    if len(parts) < 4 or parts[0] != "frames":
        raise SchemaValidationError("invalid_subject")
    lane = parts[1]
    if lane not in {"live", "replay"}:
        raise SchemaValidationError("invalid_subject_lane")
    site_id = parts[2]
    camera_id = ".".join(parts[3:])
    if not site_id or not camera_id:
        raise SchemaValidationError("invalid_subject_path")
    return lane, site_id, camera_id


def normalise_headers(headers: dict[str, str] | None) -> dict[str, str]:
    """Return a normalised string-string header mapping."""
    if not headers:
        return {}
    return {str(key): str(value) for key, value in headers.items()}


def sanitise_js_name(raw: str) -> str:
    """Convert an arbitrary identifier into a JetStream-safe token."""
    cleaned = "".join(char if char.isalnum() else "_" for char in raw.strip())
    return cleaned.strip("_") or "default"


def message_delivery_attempt(msg: Any) -> int:
    """Best-effort extraction of JetStream delivery attempt count."""
    metadata = getattr(msg, "metadata", None)
    delivered = getattr(metadata, "num_delivered", None)
    try:
        return max(1, int(delivered or 1))
    except (TypeError, ValueError):
        return 1


@dataclass(frozen=True)
class PendingBridgeMessage:
    """Inbound message state before successful Kafka produce."""

    subject: str
    payload: bytes
    headers: dict[str, str]
    site_id: str
    lane: str


@dataclass(frozen=True)
class PreparedKafkaRecord:
    """Kafka-ready payload and metadata."""

    topic: str
    key: str
    partition_key: str
    payload: bytes
    headers: dict[str, str]
    site_id: str
    lane: str


@dataclass(frozen=True)
class SpoolEnvelope:
    """Envelope written to disk for bridge recovery."""

    state: str
    timestamp_ns: int
    pending: PendingBridgeMessage | None = None
    prepared: PreparedKafkaRecord | None = None

    def to_bytes(self) -> bytes:
        """Encode the envelope as metadata JSON plus payload bytes."""
        payload = b""
        metadata: dict[str, Any] = {
            "state": self.state,
            "timestamp_ns": self.timestamp_ns,
        }
        if self.state == "pending":
            if self.pending is None:
                raise ValueError("pending envelope missing pending payload")
            metadata["pending"] = {
                "subject": self.pending.subject,
                "headers": self.pending.headers,
                "site_id": self.pending.site_id,
                "lane": self.pending.lane,
            }
            payload = self.pending.payload
        elif self.state == "prepared":
            if self.prepared is None:
                raise ValueError("prepared envelope missing prepared record")
            metadata["prepared"] = {
                "topic": self.prepared.topic,
                "key": self.prepared.key,
                "partition_key": self.prepared.partition_key,
                "headers": self.prepared.headers,
                "site_id": self.prepared.site_id,
                "lane": self.prepared.lane,
            }
            payload = self.prepared.payload
        else:
            raise ValueError(f"unknown spool envelope state: {self.state}")

        metadata_bytes = json.dumps(metadata, sort_keys=True).encode("utf-8")
        return SPOOL_HEADER.pack(len(metadata_bytes), len(payload)) + metadata_bytes + payload

    @classmethod
    def from_bytes(cls, raw: bytes) -> SpoolEnvelope:
        """Decode a spool envelope."""
        if len(raw) < SPOOL_HEADER.size:
            raise SpoolCorruptionError("spool header truncated")
        metadata_len, payload_len = SPOOL_HEADER.unpack(raw[: SPOOL_HEADER.size])
        cursor = SPOOL_HEADER.size
        metadata_raw = raw[cursor : cursor + metadata_len]
        cursor += metadata_len
        payload = raw[cursor : cursor + payload_len]
        if len(metadata_raw) != metadata_len or len(payload) != payload_len:
            raise SpoolCorruptionError("spool payload truncated")
        metadata = json.loads(metadata_raw.decode("utf-8"))
        state = metadata["state"]
        timestamp_ns = int(metadata["timestamp_ns"])
        if state == "pending":
            pending = metadata["pending"]
            return cls(
                state=state,
                timestamp_ns=timestamp_ns,
                pending=PendingBridgeMessage(
                    subject=str(pending["subject"]),
                    payload=payload,
                    headers={str(k): str(v) for k, v in pending.get("headers", {}).items()},
                    site_id=str(pending["site_id"]),
                    lane=str(pending["lane"]),
                ),
            )
        if state == "prepared":
            prepared = metadata["prepared"]
            return cls(
                state=state,
                timestamp_ns=timestamp_ns,
                prepared=PreparedKafkaRecord(
                    topic=str(prepared["topic"]),
                    key=str(prepared["key"]),
                    partition_key=str(prepared["partition_key"]),
                    payload=payload,
                    headers={str(k): str(v) for k, v in prepared.get("headers", {}).items()},
                    site_id=str(prepared["site_id"]),
                    lane=str(prepared["lane"]),
                ),
            )
        raise SpoolCorruptionError(f"unknown spool envelope state: {state}")


class LocalSpool:
    """File-backed spool for prepared or pending bridge messages."""

    def __init__(self, root: str | Path, max_bytes: int, resume_pct: int) -> None:
        self.root = Path(root)
        self.max_bytes = max_bytes
        self.resume_pct = resume_pct
        self.quarantine_dir = self.root / "quarantine"
        self.root.mkdir(parents=True, exist_ok=True)
        self.quarantine_dir.mkdir(parents=True, exist_ok=True)
        self._fill_bytes = 0
        self._message_count = 0
        self._scan_existing()

    def _scan_existing(self) -> None:
        fill_bytes = 0
        message_count = 0
        for path in self._iter_files():
            fill_bytes += path.stat().st_size
            message_count += 1
        self._fill_bytes = fill_bytes
        self._message_count = message_count
        self.refresh_metrics()

    def _iter_files(self) -> list[Path]:
        return sorted(
            (
                path
                for path in self.root.rglob("*.pb")
                if self.quarantine_dir not in path.parents
            ),
            key=lambda path: (int(path.stem) if path.stem.isdigit() else path.stem, str(path)),
        )

    @property
    def fill_bytes(self) -> int:
        return self._fill_bytes

    @property
    def fill_pct(self) -> float:
        if self.max_bytes <= 0:
            return 0.0
        return (self._fill_bytes / self.max_bytes) * 100.0

    @property
    def message_count(self) -> int:
        return self._message_count

    @property
    def is_full(self) -> bool:
        return self._fill_bytes >= self.max_bytes

    @property
    def can_resume(self) -> bool:
        return self.fill_pct < float(self.resume_pct)

    def refresh_metrics(self) -> None:
        SPOOL_DEPTH_BYTES.set(self._fill_bytes)
        SPOOL_DEPTH_MESSAGES.set(self._message_count)
        SPOOL_FILL_PCT.set(self.fill_pct)

    def _target_path(self, envelope: SpoolEnvelope) -> Path:
        if envelope.state == "prepared" and envelope.prepared is not None:
            topic = envelope.prepared.topic
            partition_key = envelope.prepared.partition_key
        elif envelope.state == "pending" and envelope.pending is not None:
            topic = "pending"
            partition_key = envelope.pending.site_id
        else:
            raise ValueError("invalid spool envelope")
        return self.root / topic / partition_key / f"{envelope.timestamp_ns}.pb"

    async def enqueue(self, envelope: SpoolEnvelope) -> Path:
        raw = envelope.to_bytes()
        path = self._target_path(envelope)
        path.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(self._write_file, path, raw)
        self._fill_bytes += len(raw)
        self._message_count += 1
        self.refresh_metrics()
        return path

    def _write_file(self, path: Path, raw: bytes) -> None:
        with open(path, "wb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())

    async def delete(self, path: Path) -> None:
        size = path.stat().st_size if path.exists() else 0
        await asyncio.to_thread(path.unlink, missing_ok=True)
        self._fill_bytes = max(0, self._fill_bytes - size)
        self._message_count = max(0, self._message_count - 1)
        self.refresh_metrics()

    async def read(self, path: Path) -> SpoolEnvelope:
        raw = await asyncio.to_thread(path.read_bytes)
        return SpoolEnvelope.from_bytes(raw)

    async def move_to_quarantine(self, path: Path) -> None:
        target = self.quarantine_dir / path.name
        target.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(path.replace, target)
        size = target.stat().st_size if target.exists() else 0
        self._fill_bytes = max(0, self._fill_bytes - size)
        self._message_count = max(0, self._message_count - 1)
        self.refresh_metrics()

    def files(self) -> list[Path]:
        return self._iter_files()


class AsyncTokenBucket:
    """Small async token-bucket limiter."""

    def __init__(self, rate_per_sec: float) -> None:
        self.rate_per_sec = max(rate_per_sec, 0.1)
        self.capacity = max(self.rate_per_sec, 1.0)
        self.tokens = self.capacity
        self.updated_at = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> float:
        async with self._lock:
            waited_s = 0.0
            while True:
                now = time.monotonic()
                elapsed = now - self.updated_at
                self.updated_at = now
                self.tokens = min(self.capacity, self.tokens + elapsed * self.rate_per_sec)
                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    return waited_s
                wait_time = (1.0 - self.tokens) / self.rate_per_sec
                waited_s += wait_time
                await asyncio.sleep(wait_time)


class SiteTrafficTracker:
    """Tracks recent live traffic per site to keep drain traffic secondary."""

    def __init__(self) -> None:
        self._events: dict[str, deque[float]] = {}

    def note_live(self, site_id: str) -> None:
        bucket = self._events.setdefault(site_id, deque())
        now = time.monotonic()
        bucket.append(now)
        self._trim(site_id, now)

    def rate_per_sec(self, site_id: str) -> float:
        now = time.monotonic()
        self._trim(site_id, now)
        return float(len(self._events.get(site_id, ())))

    def _trim(self, site_id: str, now: float) -> None:
        bucket = self._events.setdefault(site_id, deque())
        while bucket and now - bucket[0] > 1.0:
            bucket.popleft()


class NatsAdapterProtocol(Protocol):
    """Runtime interface for the JetStream adapter."""

    @property
    def is_connected(self) -> bool:
        """Return True when the NATS transport is currently connected."""

    async def ensure_connected(self) -> None:
        """Connect or wait for reconnection with bounded backoff."""

    async def fetch_batch(self) -> list[NatsMessageProtocol]:
        """Fetch a small batch of JetStream messages."""

    async def publish_dlq(self, pending: PendingBridgeMessage, reason: str) -> None:
        """Publish a rejected payload to the per-site DLQ subject."""

    async def close(self) -> None:
        """Close any open NATS resources."""


class NatsJetStreamAdapter:
    """Binds to pre-created per-site JetStream durables and fetches messages."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._nc: Any | None = None
        self._js: Any | None = None
        self._subscriptions: dict[tuple[str, str], Any] = {}
        self._connect_lock = asyncio.Lock()
        self._closed = False

    @property
    def is_connected(self) -> bool:
        return self._nc is not None and bool(getattr(self._nc, "is_connected", False))

    async def ensure_connected(self) -> None:
        if self.is_connected and self._subscriptions:
            return
        async with self._connect_lock:
            if self.is_connected and self._subscriptions:
                return
            await self._connect_with_backoff()

    async def _connect_with_backoff(self) -> None:
        delay_s = 1.0
        while not self._closed:
            try:
                await self._connect_once()
                return
            except Exception as exc:  # pragma: no cover - depends on runtime services
                logger.warning(
                    "NATS connect/bind failed; retrying in %.1fs: %s",
                    delay_s,
                    exc,
                )
                await asyncio.sleep(delay_s)
                delay_s = min(delay_s * 2.0, 30.0)
        raise RuntimeError("nats_adapter_closed")

    async def _connect_once(self) -> None:
        try:
            import nats  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover - depends on local env
            raise RuntimeError("missing optional dependency 'nats-py'; install requirements.txt") from exc

        tls_context = build_nats_ssl_context(self.settings)
        self._nc = await nats.connect(
            self.settings.nats.url,
            tls=tls_context,
            max_reconnect_attempts=-1,
            reconnect_time_wait=2,
            disconnected_cb=self._on_disconnect,
            reconnected_cb=self._on_reconnect,
            error_cb=self._on_error,
        )
        self._js = self._nc.jetstream()
        await self._bind_site_consumers()
        logger.info("Connected to NATS JetStream at %s", self.settings.nats.url)

    async def _bind_site_consumers(self) -> None:
        if self._js is None:
            raise RuntimeError("jetstream_not_initialized")
        subscriptions: dict[tuple[str, str], Any] = {}
        for site in self.settings.sites:
            for lane, subject_template in (
                ("live", self.settings.nats.live_subject_template),
                ("replay", self.settings.nats.replay_subject_template),
            ):
                subject = subject_template.format(site_id=site.site_id)
                durable = sanitise_js_name(f"{self.settings.nats.durable_prefix}_{site.site_id}_{lane}")
                kwargs: dict[str, Any] = {"durable": durable}
                stream_name = self._stream_name(site.site_id)
                if stream_name:
                    kwargs["stream"] = stream_name
                subscriptions[(site.site_id, lane)] = await self._js.pull_subscribe(subject, **kwargs)
                await self._update_consumer_lag(site.site_id, lane, subscriptions[(site.site_id, lane)])
        self._subscriptions = subscriptions

    def _stream_name(self, site_id: str) -> str | None:
        template = self.settings.nats.stream_name_template
        if template:
            return template.format(
                site_id=site_id,
                site_token=sanitise_js_name(site_id).upper(),
            )
        return f"CILEX_{sanitise_js_name(site_id).upper()}"

    async def _update_consumer_lag(self, site_id: str, lane: str, subscription: Any) -> None:
        try:
            info = await subscription.consumer_info()
        except Exception:  # pragma: no cover - depends on runtime services
            return
        pending = int(getattr(info, "num_pending", 0) or 0)
        ack_pending = int(getattr(info, "num_ack_pending", 0) or 0)
        NATS_CONSUMER_LAG.labels(site_id=site_id, lane=lane).set(float(pending + ack_pending))

    async def fetch_batch(self) -> list[NatsMessageProtocol]:
        if not self.is_connected:
            return []
        messages: list[NatsMessageProtocol] = []
        for (site_id, lane), subscription in self._subscriptions.items():
            try:
                batch = await subscription.fetch(
                    batch=self.settings.nats.fetch_batch_size,
                    timeout=self.settings.nats.fetch_timeout_s,
                )
            except TimeoutError:
                batch = []
            except Exception as exc:  # pragma: no cover - depends on runtime services
                logger.warning(
                    "JetStream fetch failed for site=%s lane=%s: %s",
                    site_id,
                    lane,
                    exc,
                )
                raise
            await self._update_consumer_lag(site_id, lane, subscription)
            messages.extend(batch)
        return messages

    async def publish_dlq(self, pending: PendingBridgeMessage, reason: str) -> None:
        if self._nc is None:
            raise RuntimeError("nats_not_connected")
        subject = self.settings.nats.dlq_subject_template.format(site_id=pending.site_id)
        headers = {
            "X-DLQ-Reason": reason,
            "X-Original-Subject": pending.subject,
            "X-Original-Lane": pending.lane,
        }
        await self._nc.publish(subject, pending.payload, headers=headers)
        await self._nc.flush()

    async def close(self) -> None:
        self._closed = True
        subscriptions = self._subscriptions
        self._subscriptions = {}
        for (site_id, lane), _subscription in subscriptions.items():
            NATS_CONSUMER_LAG.labels(site_id=site_id, lane=lane).set(0.0)
        if self._nc is not None:
            await self._nc.close()
        self._nc = None
        self._js = None

    async def _on_disconnect(self) -> None:
        logger.warning("NATS disconnected")

    async def _on_reconnect(self) -> None:
        logger.info("NATS reconnected")
        if self.settings.sites and not self._subscriptions:
            with contextlib.suppress(Exception):
                await self._bind_site_consumers()

    async def _on_error(self, exc: Exception) -> None:
        logger.error("NATS client error: %s", exc)


class SchemaRegistryValidator:
    """Validates that the FrameRef schema is known to the registry, then parses payloads."""

    def __init__(
        self,
        base_url: str,
        subject: str,
        cache_ttl_s: int = 300,
        fetcher: Callable[[str], dict[str, Any]] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.subject = subject
        self.cache_ttl_s = cache_ttl_s
        self._fetcher = fetcher or self._default_fetcher
        self._cached_schema: dict[str, Any] | None = None
        self._cached_at = 0.0
        self._deserializer: Callable[[bytes], Any] | None = None

    def _default_fetcher(self, url: str) -> dict[str, Any]:
        with urllib.request.urlopen(url, timeout=5) as response:  # noqa: S310
            return json.loads(response.read().decode("utf-8"))

    def _ensure_schema_available(self) -> dict[str, Any]:
        now = time.monotonic()
        if self._cached_schema and now - self._cached_at < self.cache_ttl_s:
            return self._cached_schema
        url = f"{self.base_url}/subjects/{urllib.parse.quote(self.subject, safe='')}/versions/latest"
        try:
            payload = self._fetcher(url)
        except (urllib.error.URLError, TimeoutError, ValueError) as exc:
            if self._cached_schema is not None:
                logger.warning("Schema Registry unavailable, using cached FrameRef schema: %s", exc)
                return self._cached_schema
            raise SchemaValidationError("schema_registry_unavailable") from exc
        self._cached_schema = payload
        self._cached_at = now
        return payload

    def _build_confluent_deserializer(self) -> Callable[[bytes], Any] | None:
        try:
            from confluent_kafka.schema_registry import SchemaRegistryClient  # noqa: PLC0415
            from confluent_kafka.schema_registry.protobuf import ProtobufDeserializer  # noqa: PLC0415
            from confluent_kafka.serialization import MessageField, SerializationContext  # noqa: PLC0415
        except ImportError:
            return None

        frame_type = load_frame_ref_type()
        client = SchemaRegistryClient({"url": self.base_url})
        try:
            deserializer = ProtobufDeserializer(
                frame_type,
                schema_registry_client=client,
            )
        except TypeError:  # pragma: no cover - depends on confluent-kafka version
            deserializer = ProtobufDeserializer(frame_type, {}, client)

        def _deserialize(payload: bytes) -> Any:
            context = SerializationContext(self.subject, MessageField.VALUE)
            return deserializer(payload, context)

        return _deserialize

    def _deserialize_frame(self, payload: bytes) -> Any:
        if self._deserializer is None:
            self._deserializer = self._build_confluent_deserializer() or self._deserialize_generated
        try:
            frame = self._deserializer(payload)
            if frame is not None:
                return frame
        except Exception as exc:
            if self._deserializer is not self._deserialize_generated:
                logger.debug(
                    "Confluent protobuf deserializer rejected payload; "
                    "falling back to generated FrameRef parse: %s",
                    exc,
                )
                return self._deserialize_generated(payload)
            raise SchemaValidationError("protobuf_deserialize_failed") from exc
        return self._deserialize_generated(payload)

    def _deserialize_generated(self, payload: bytes) -> Any:
        frame_cls = load_frame_ref_type()
        frame = frame_cls()
        try:
            frame.ParseFromString(payload)
        except Exception as exc:  # pragma: no cover - protobuf implementation detail
            raise SchemaValidationError("protobuf_deserialize_failed") from exc
        return frame

    def validate_frame(self, payload: bytes) -> Any:
        schema = self._ensure_schema_available()
        schema_text = str(schema.get("schema", ""))
        if "message FrameRef" not in schema_text and "FrameRef" not in self.subject:
            raise SchemaValidationError("schema_subject_mismatch")

        frame = self._deserialize_frame(payload)

        if not getattr(frame, "frame_id", ""):
            raise SchemaValidationError("missing_frame_id")
        if not getattr(frame, "camera_id", ""):
            raise SchemaValidationError("missing_camera_id")
        if not getattr(frame, "frame_uri", ""):
            raise SchemaValidationError("missing_frame_uri")
        if int(getattr(frame, "frame_sequence", 0)) <= 0:
            raise SchemaValidationError("missing_frame_sequence")

        frame_uri = str(frame.frame_uri)
        if not (frame_uri.startswith("s3://") or frame_uri.startswith("minio://")):
            raise SchemaValidationError("invalid_frame_uri")
        if frame_uri.startswith("data:") or "base64," in frame_uri:
            raise SchemaValidationError("raw_bytes_uri_forbidden")

        timestamps = getattr(frame, "timestamps", None)
        if timestamps is None:
            raise SchemaValidationError("missing_timestamps")
        if not timestamp_is_set(getattr(timestamps, "source_capture_ts", None)):
            raise SchemaValidationError("missing_source_capture_ts")
        if not timestamp_is_set(getattr(timestamps, "edge_receive_ts", None)):
            raise SchemaValidationError("missing_edge_receive_ts")
        return frame


class MinioBlobOffloader:
    """Uploads large auxiliary blobs to MinIO and returns URI references."""

    def __init__(
        self,
        endpoint: str,
        access_key: str,
        secret_key: str,
        bucket: str,
        secure: bool,
    ) -> None:
        self.endpoint = endpoint
        self.access_key = access_key
        self.secret_key = secret_key
        self.bucket = bucket
        self.secure = secure
        self._client: Any | None = None

    def _client_instance(self) -> Any:
        if self._client is None:
            try:
                from minio import Minio  # noqa: PLC0415
            except ImportError as exc:  # pragma: no cover - depends on local env
                raise RuntimeError("missing optional dependency 'minio'; install requirements.txt") from exc
            self._client = Minio(
                self.endpoint,
                access_key=self.access_key,
                secret_key=self.secret_key,
                secure=self.secure,
            )
        return self._client

    async def is_reachable(self) -> bool:
        try:
            client = self._client_instance()
            return await asyncio.to_thread(client.bucket_exists, self.bucket)
        except Exception:
            return False

    async def offload(self, site_id: str, camera_id: str, name: str, data: bytes) -> str:
        client = self._client_instance()
        digest_hex = md5(data).hexdigest()  # noqa: S324 - integrity, not security
        object_name = f"{site_id}/{camera_id}/{sha256(name.encode('utf-8') + data).hexdigest()}"
        for attempt in range(1, 4):
            start = time.monotonic()
            try:
                await asyncio.to_thread(
                    client.put_object,
                    self.bucket,
                    object_name,
                    io.BytesIO(data),
                    len(data),
                    "application/octet-stream",
                )
            except Exception:
                if attempt >= 3:
                    raise
                await asyncio.sleep(1.0)
                continue
            BLOB_OFFLOAD.labels(site_id=site_id, bucket=self.bucket).inc()
            BLOB_OFFLOAD_LATENCY.labels(bucket=self.bucket).observe((time.monotonic() - start) * 1000.0)
            # MinIO's etag for small single-part uploads is the MD5 digest.
            logger.debug("Offloaded auxiliary blob %s with md5=%s", object_name, digest_hex)
            return f"minio://{self.bucket}/{object_name}"
        raise RuntimeError("minio_offload_failed")


class KafkaProducerAdapter:
    """Thin async wrapper over the confluent-kafka producer."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._producer: Any | None = None

    def _create_ssl_context(self) -> dict[str, str]:
        conf: dict[str, str] = {}
        if self.settings.kafka.ssl_ca_file:
            conf["ssl.ca.location"] = self.settings.kafka.ssl_ca_file
        if self.settings.kafka.ssl_cert_file:
            conf["ssl.certificate.location"] = self.settings.kafka.ssl_cert_file
        if self.settings.kafka.ssl_key_file:
            conf["ssl.key.location"] = self.settings.kafka.ssl_key_file
        return conf

    def _producer_instance(self) -> Any:
        if self._producer is None:
            try:
                from confluent_kafka import Producer  # noqa: PLC0415
            except ImportError as exc:  # pragma: no cover - depends on local env
                raise RuntimeError(
                    "missing optional dependency 'confluent_kafka'; install requirements.txt"
                ) from exc
            conf: dict[str, Any] = {
                "bootstrap.servers": self.settings.kafka.bootstrap_servers,
                "security.protocol": self.settings.kafka.security_protocol,
                "sasl.mechanism": self.settings.kafka.sasl_mechanism,
                "sasl.username": self.settings.kafka.sasl_username,
                "sasl.password": self.settings.kafka.sasl_password,
                "client.id": self.settings.kafka.client_id,
                "acks": self.settings.kafka.acks,
                "compression.type": self.settings.kafka.compression_type,
                "linger.ms": self.settings.kafka.linger_ms,
                "batch.size": self.settings.kafka.batch_size,
                "enable.idempotence": self.settings.kafka.enable_idempotence,
                "request.timeout.ms": self.settings.kafka.request_timeout_ms,
            }
            conf.update(self._create_ssl_context())
            self._producer = Producer(conf)
        return self._producer

    async def is_ready(self) -> bool:
        try:
            self._producer_instance()
            return True
        except Exception:
            return False

    async def _poll_until_complete(self, producer: Any, future: asyncio.Future[None]) -> None:
        while not future.done():
            await asyncio.to_thread(producer.poll, 0.1)

    async def produce(self, record: PreparedKafkaRecord) -> None:
        start = time.monotonic()
        producer = self._producer_instance()
        loop = asyncio.get_running_loop()
        future: asyncio.Future[None] = loop.create_future()

        def _delivery(err: Any, _msg: Any) -> None:
            if future.done():
                return
            if err is not None:
                loop.call_soon_threadsafe(future.set_exception, RuntimeError(str(err)))
            else:
                loop.call_soon_threadsafe(future.set_result, None)

        KAFKA_INFLIGHT.inc()
        poll_task: asyncio.Task[None] | None = None
        try:
            producer.produce(
                topic=record.topic,
                key=record.key,
                value=record.payload,
                headers=list(record.headers.items()),
                on_delivery=_delivery,
            )
            poll_task = asyncio.create_task(self._poll_until_complete(producer, future))
            await asyncio.wait_for(future, timeout=self.settings.kafka.request_timeout_ms / 1000.0)
            elapsed_ms = (time.monotonic() - start) * 1000.0
            KAFKA_PRODUCE_LATENCY.labels(topic=record.topic).observe(elapsed_ms)
        finally:
            if poll_task is not None:
                poll_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await poll_task
            KAFKA_INFLIGHT.dec()


class IngressBridgeService:
    """Core bridge coordinator."""

    def __init__(
        self,
        settings: Settings,
        *,
        schema_validator: SchemaRegistryValidator | None = None,
        blob_offloader: MinioBlobOffloader | None = None,
        kafka_producer: KafkaProducerAdapter | Any | None = None,
        nats_adapter: NatsAdapterProtocol | None = None,
        spool: LocalSpool | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.settings = settings
        self.schema_validator = schema_validator or SchemaRegistryValidator(
            settings.schema_registry.url,
            settings.schema_registry.frame_ref_subject,
            settings.schema_registry.cache_ttl_s,
        )
        self.blob_offloader = blob_offloader or MinioBlobOffloader(
            settings.minio.endpoint,
            settings.minio.access_key,
            settings.minio.secret_key,
            settings.minio.bucket_blobs,
            settings.minio.secure,
        )
        self.kafka_producer = kafka_producer or KafkaProducerAdapter(settings)
        self.nats_adapter = nats_adapter or NatsJetStreamAdapter(settings)
        self.spool = spool or LocalSpool(
            settings.spool.path,
            settings.spool.max_bytes,
            settings.spool.resume_pct,
        )
        self.clock = clock or time.time
        self.site_config = settings.site_index()
        self.live_limiters: dict[str, AsyncTokenBucket] = {}
        self.replay_limiters: dict[str, AsyncTokenBucket] = {}
        self.drain_limiters: dict[str, AsyncTokenBucket] = {}
        self.live_tracker = SiteTrafficTracker()
        self.nats_connected = False
        self._shutdown = asyncio.Event()
        self._drain_task: asyncio.Task[None] | None = None
        self._consume_task: asyncio.Task[None] | None = None
        self._init_limiters()
        self.spool.refresh_metrics()

    def _init_limiters(self) -> None:
        for site in self.settings.sites:
            live_limit = float(site.rate_limit_msg_per_sec)
            replay_limit = float(site.replay_rate_limit_msg_per_sec or min(
                self.settings.spool.replay_rate_limit_msg_per_sec,
                int(live_limit * self.settings.spool.replay_limit_pct / 100.0),
            ))
            drain_limit = float(site.spool_drain_rate_limit_msg_per_sec or min(
                self.settings.spool.spool_drain_rate_limit_msg_per_sec,
                int(live_limit * self.settings.spool.spool_drain_pct / 100.0),
            ))
            self.live_limiters[site.site_id] = AsyncTokenBucket(live_limit)
            self.replay_limiters[site.site_id] = AsyncTokenBucket(max(replay_limit, 1.0))
            self.drain_limiters[site.site_id] = AsyncTokenBucket(max(drain_limit, 1.0))
            RATE_LIMIT_HEADROOM.labels(site_id=site.site_id).set(100.0)

    def _site_or_default(self, site_id: str) -> SiteConfig:
        return self.site_config.get(site_id, SiteConfig(site_id=site_id))

    def _update_headroom(self, site_id: str) -> None:
        site = self._site_or_default(site_id)
        live_rate = self.live_tracker.rate_per_sec(site_id)
        headroom = max(0.0, 100.0 * (1.0 - min(live_rate / max(site.rate_limit_msg_per_sec, 1), 1.0)))
        RATE_LIMIT_HEADROOM.labels(site_id=site_id).set(headroom)

    async def is_ready(self) -> bool:
        return (
            self.nats_connected
            and await self.kafka_producer.is_ready()
            and await self.blob_offloader.is_reachable()
        )

    def make_kafka_key(self, site_id: str, frame: Any) -> str:
        capture_epoch_us = timestamp_to_epoch_us(frame.timestamps.source_capture_ts)
        return f"{site_id}:{frame.camera_id}:{capture_epoch_us}:{int(frame.frame_sequence)}"

    def _encode_inline_header(self, value: str) -> str:
        return value

    async def _offload_auxiliary_headers(
        self,
        site_id: str,
        camera_id: str,
        headers: dict[str, str],
    ) -> dict[str, str]:
        kafka_headers: dict[str, str] = {}
        for key, value in headers.items():
            if not key.startswith(AUXILIARY_HEADER_PREFIX):
                kafka_headers[key.lower()] = self._encode_inline_header(value)
                continue
            name = key[len(AUXILIARY_HEADER_PREFIX) :].lower()
            try:
                blob = base64.b64decode(value.encode("utf-8"), validate=True)
            except Exception:
                raise SchemaValidationError("invalid_aux_blob_base64") from None
            if len(blob) > OFFLOAD_THRESHOLD_BYTES:
                uri = await self.blob_offloader.offload(site_id, camera_id, name, blob)
                kafka_headers[f"x-blob-ref-{name}"] = uri
            else:
                kafka_headers[f"x-blob-inline-{name}"] = base64.b64encode(blob).decode("utf-8")
        return kafka_headers

    def stamp_core_ingest(self, frame: Any, site_id: str) -> None:
        now = self.clock()
        set_timestamp_from_epoch(frame.timestamps.core_ingest_ts, now)
        edge_us = timestamp_to_epoch_us(frame.timestamps.edge_receive_ts)
        core_us = timestamp_to_epoch_us(frame.timestamps.core_ingest_ts)
        if core_us < edge_us:
            CLOCK_DRIFT.labels(site_id=site_id).inc()
            logger.warning(
                "core_ingest_ts earlier than edge_receive_ts for site=%s camera=%s",
                site_id,
                frame.camera_id,
            )

    async def prepare_record(self, pending: PendingBridgeMessage) -> PreparedKafkaRecord:
        frame = self.schema_validator.validate_frame(pending.payload)
        self.stamp_core_ingest(frame, pending.site_id)
        key = self.make_kafka_key(pending.site_id, frame)
        kafka_headers = await self._offload_auxiliary_headers(
            pending.site_id,
            frame.camera_id,
            pending.headers,
        )
        kafka_headers.setdefault("x-site-id", pending.site_id)
        kafka_headers.setdefault("x-lane", pending.lane)
        kafka_headers.setdefault("x-camera-id", str(frame.camera_id))
        kafka_headers.setdefault("x-idempotent-key", key)
        return PreparedKafkaRecord(
            topic=self.settings.kafka.topic_frames_sampled_refs,
            key=key,
            partition_key=str(frame.camera_id),
            payload=frame.SerializeToString(),
            headers=kafka_headers,
            site_id=pending.site_id,
            lane=pending.lane,
        )

    async def _spool_prepared(self, record: PreparedKafkaRecord, reason: str) -> None:
        envelope = SpoolEnvelope(
            state="prepared",
            timestamp_ns=time.time_ns(),
            prepared=record,
        )
        await self.spool.enqueue(envelope)
        if self.spool.is_full:
            SPOOL_FULL.inc()
        MESSAGES_SPOOLED.labels(site_id=record.site_id, reason=reason).inc()

    async def _spool_pending(self, pending: PendingBridgeMessage, reason: str) -> None:
        envelope = SpoolEnvelope(
            state="pending",
            timestamp_ns=time.time_ns(),
            pending=pending,
        )
        await self.spool.enqueue(envelope)
        if self.spool.is_full:
            SPOOL_FULL.inc()
        MESSAGES_SPOOLED.labels(site_id=pending.site_id, reason=reason).inc()

    async def _produce(self, record: PreparedKafkaRecord) -> None:
        start = time.monotonic()
        await self.kafka_producer.produce(record)
        elapsed_ms = (time.monotonic() - start) * 1000.0
        NATS_TO_KAFKA_LATENCY.labels(site_id=record.site_id, lane=record.lane).observe(elapsed_ms)
        MESSAGES_PRODUCED.labels(site_id=record.site_id, topic=record.topic).inc()

    async def handle_message(self, msg: NatsMessageProtocol, delivery_attempt: int = 1) -> str:
        lane, site_id, _camera_id = parse_subject(msg.subject)
        MESSAGES_RECEIVED.labels(site_id=site_id, lane=lane).inc()

        if self.spool.is_full and not self.spool.can_resume:
            logger.warning("Spool full; leaving message unacked for site=%s lane=%s", site_id, lane)
            return "paused"

        if lane == "live":
            self.live_tracker.note_live(site_id)
            self._update_headroom(site_id)
            waited_s = await self.live_limiters.setdefault(
                site_id,
                AsyncTokenBucket(self._site_or_default(site_id).rate_limit_msg_per_sec),
            ).acquire()
        else:
            waited_s = await self.replay_limiters.setdefault(site_id, AsyncTokenBucket(1.0)).acquire()

        if waited_s > 0:
            RATE_LIMITED.labels(site_id=site_id, lane=lane).inc()

        pending = PendingBridgeMessage(
            subject=msg.subject,
            payload=msg.data,
            headers=normalise_headers(getattr(msg, "headers", None)),
            site_id=site_id,
            lane=lane,
        )
        try:
            prepared = await self.prepare_record(pending)
        except SchemaValidationError as exc:
            SCHEMA_REJECTION.labels(site_id=site_id, reason=exc.reason).inc()
            if delivery_attempt >= self.settings.nats.max_redeliver:
                try:
                    await self.publish_dlq(pending, exc.reason)
                except Exception as dlq_exc:
                    logger.warning(
                        "DLQ publish failed for site=%s reason=%s: %s",
                        site_id,
                        exc.reason,
                        dlq_exc,
                        exc_info=True,
                    )
                    await msg.nak()
                    return "nack"
                await msg.ack()
                return "dlq"
            await msg.nak()
            return "nack"
        except Exception as exc:
            logger.warning("Preparation failed; spooling pending message: %s", exc, exc_info=True)
            await self._spool_pending(pending, reason="prepare_failure")
            await msg.ack()
            return "spooled_pending"

        try:
            await self._produce(prepared)
        except Exception as exc:
            logger.warning("Kafka produce failed; spooling prepared record: %s", exc, exc_info=True)
            await self._spool_prepared(prepared, reason="kafka_failure")
            await msg.ack()
            return "spooled_prepared"

        await msg.ack()
        return "produced"

    async def publish_dlq(self, pending: PendingBridgeMessage, reason: str) -> None:
        if self.nats_adapter is not None:
            await self.nats_adapter.publish_dlq(pending, reason)
        logger.error("Publishing message to DLQ for site=%s reason=%s", pending.site_id, reason)
        DLQ_PUBLISHED.labels(site_id=pending.site_id).inc()

    async def process_spool_file(self, path: Path) -> bool:
        try:
            envelope = await self.spool.read(path)
        except SpoolCorruptionError:
            SPOOL_CORRUPT.inc()
            await self.spool.move_to_quarantine(path)
            return False

        if envelope.state == "pending" and envelope.pending is not None:
            site_id = envelope.pending.site_id
            if self.live_tracker.rate_per_sec(site_id) >= self._site_or_default(site_id).rate_limit_msg_per_sec * 0.8:
                return False
            await self.drain_limiters.setdefault(site_id, AsyncTokenBucket(1.0)).acquire()
            prepared = await self.prepare_record(envelope.pending)
            await self._produce(prepared)
            await self.spool.delete(path)
            SPOOL_DRAINED.labels(site_id=site_id).inc()
            return True

        if envelope.state == "prepared" and envelope.prepared is not None:
            site_id = envelope.prepared.site_id
            if self.live_tracker.rate_per_sec(site_id) >= self._site_or_default(site_id).rate_limit_msg_per_sec * 0.8:
                return False
            await self.drain_limiters.setdefault(site_id, AsyncTokenBucket(1.0)).acquire()
            await self._produce(envelope.prepared)
            await self.spool.delete(path)
            SPOOL_DRAINED.labels(site_id=site_id).inc()
            return True

        raise SpoolCorruptionError("unsupported spool envelope")

    async def drain_once(self) -> int:
        drained = 0
        for path in self.spool.files():
            try:
                success = await self.process_spool_file(path)
            except Exception as exc:
                logger.warning("Failed to drain spool file %s: %s", path, exc, exc_info=True)
                break
            if not success:
                continue
            drained += 1
        return drained

    async def drain_loop(self) -> None:
        while not self._shutdown.is_set():
            if self.spool.message_count == 0:
                await asyncio.sleep(0.5)
                continue
            await self.drain_once()
            await asyncio.sleep(0.1)

    async def consume_once(self) -> int:
        if self.nats_adapter is None:
            return 0
        if self.spool.is_full and not self.spool.can_resume:
            self.nats_connected = self.nats_adapter.is_connected
            return 0
        await self.nats_adapter.ensure_connected()
        self.nats_connected = self.nats_adapter.is_connected
        messages = await self.nats_adapter.fetch_batch()
        processed = 0
        for msg in messages:
            await self.handle_message(msg, delivery_attempt=message_delivery_attempt(msg))
            processed += 1
        return processed

    async def consume_loop(self) -> None:
        while not self._shutdown.is_set():
            try:
                processed = await self.consume_once()
            except Exception as exc:
                self.nats_connected = False
                logger.warning("JetStream consume loop failed: %s", exc, exc_info=True)
                await asyncio.sleep(1.0)
                continue
            if processed == 0:
                await asyncio.sleep(0.1)

    async def start_background_tasks(self) -> None:
        if self._drain_task is None:
            self._drain_task = asyncio.create_task(self.drain_loop(), name="spool-drain")
        if self._consume_task is None:
            self._consume_task = asyncio.create_task(self.consume_loop(), name="nats-consume")

    async def shutdown(self) -> None:
        self._shutdown.set()
        for task in (self._consume_task, self._drain_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._consume_task = None
        self._drain_task = None
        self.nats_connected = False
        if self.nats_adapter is not None:
            await self.nats_adapter.close()


def create_app(service: IngressBridgeService) -> Any:
    """Build the FastAPI app lazily so unit tests do not require FastAPI."""
    try:
        from fastapi import FastAPI, Response  # noqa: PLC0415
        from prometheus_client import CONTENT_TYPE_LATEST, generate_latest  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - depends on local env
        raise RuntimeError("missing optional dependency 'fastapi'; install requirements.txt") from exc

    app = FastAPI(title="Ingress Bridge", version="1.0.0")

    @app.on_event("startup")
    async def _startup() -> None:
        await service.start_background_tasks()

    @app.on_event("shutdown")
    async def _shutdown() -> None:
        await service.shutdown()

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ready")
    async def ready() -> Response:
        if await service.is_ready():
            return Response(content=json.dumps({"status": "ready"}), media_type="application/json", status_code=200)
        return Response(content=json.dumps({"status": "not-ready"}), media_type="application/json", status_code=503)

    @app.get("/metrics")
    async def metrics() -> Response:
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return app


def build_nats_ssl_context(settings: Settings) -> ssl.SSLContext | None:
    """Create an mTLS client context for NATS when configured."""
    if settings.nats.tls is None:
        return None
    context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
    context.load_verify_locations(settings.nats.tls.ca_file)
    context.load_cert_chain(settings.nats.tls.cert_file, settings.nats.tls.key_file)
    return context


async def run_http(service: IngressBridgeService, settings: Settings) -> None:
    """Run the FastAPI app with uvicorn."""
    try:
        import uvicorn  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - depends on local env
        raise RuntimeError("missing optional dependency 'uvicorn'; install requirements.txt") from exc
    app = create_app(service)
    config = uvicorn.Config(app, host="0.0.0.0", port=settings.health_port, log_level=settings.log_level.lower())
    server = uvicorn.Server(config)
    await server.serve()


async def async_main() -> None:
    """Entrypoint used by __main__."""
    args = parse_args()
    settings = Settings.from_yaml(args.config)
    setup_logging(settings.log_level)
    service = IngressBridgeService(settings)
    await run_http(service, settings)


if __name__ == "__main__":
    try:
        asyncio.run(async_main())
    except Exception as exc:  # pragma: no cover - CLI boundary
        raise SystemExit(str(exc)) from exc
