"""Event engine service.

Consumes tracklets from Kafka, maintains per-track state machines,
detects rule-based events, publishes Event protobufs to Kafka, and writes
event rows to PostgreSQL.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, cast

import asyncpg

from clip_uri import build_segment_range_uri
from config import EventEngineSettings
from event_emitter import EventEmitter
from metadata_aggregator import MetadataAggregator
from metrics import (
    EVENT_ACTIVE_STATE_MACHINES,
    EVENT_STATE_TRANSITIONS_TOTAL,
    EVENT_SUPPRESSED_TOTAL,
    EVENT_TRACKLETS_CONSUMED_TOTAL,
)
from state_machine import (
    CameraZones,
    EventTimestamps,
    EventTrigger,
    EventType,
    TrackStateMachine,
    extract_event_timestamps,
    extract_tracklet_time,
)

logger = logging.getLogger(__name__)

PROTO_PATH = Path(__file__).resolve().parent / "proto_gen"
if str(PROTO_PATH) not in sys.path:
    sys.path.insert(0, str(PROTO_PATH))

DEFAULT_CONFIG = Path(__file__).resolve().parent / "config.yaml"

OBJECT_CLASS_NAMES = {
    0: "unspecified",
    1: "person",
    2: "car",
    3: "truck",
    4: "bus",
    5: "bicycle",
    6: "motorcycle",
    7: "animal",
}

TRACKLET_STATE_NEW = 1
TRACKLET_STATE_ACTIVE = 2
TRACKLET_STATE_LOST = 3
TRACKLET_STATE_TERMINATED = 4


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help="YAML config path.",
    )
    return parser.parse_args()


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def _load_tracklet_type() -> type[Any]:
    try:
        from vidanalytics.v1.tracklet import tracklet_pb2  # noqa: PLC0415
    except ImportError as exc:
        raise RuntimeError(
            "generated protobufs not found; run `bash gen_proto.sh`"
        ) from exc
    return cast(type[Any], tracklet_pb2.Tracklet)


@dataclass
class CameraMotionState:
    """Best-effort camera activity derived from tracklet flow.

    When motion_active is true, active_event_id points at the open
    motion duration event row being accumulated for this camera.
    """

    motion_active: bool = False
    last_activity_at: float | None = None
    last_timestamps: Any = None
    active_event_id: str | None = None
    motion_started_at: datetime | None = None


class CameraConfigStore:
    """Lazy camera-config loader from the `cameras` table."""

    def __init__(
        self,
        pool: asyncpg.Pool,
        default_loitering_duration_s: float,
    ) -> None:
        self._pool = pool
        self._default_loitering_duration_s = default_loitering_duration_s
        self._cache: dict[str, CameraZones] = {}

    async def get(self, camera_id: str) -> CameraZones:
        cached = self._cache.get(camera_id)
        if cached is not None:
            return cached

        row = await self._pool.fetchrow(
            "SELECT config_json FROM cameras WHERE camera_id = $1",
            camera_id,
        )
        camera_zones = CameraZones.from_camera_config(
            row["config_json"] if row is not None else None,
            default_loitering_duration_s=self._default_loitering_duration_s,
        )
        self._cache[camera_id] = camera_zones
        return camera_zones


class EventEngineService:
    """Service orchestrator for the event engine."""

    _DEDUP_EVENT_TYPES = {
        EventType.ENTERED_SCENE.value,
        EventType.EXITED_SCENE.value,
    }

    def __init__(self, settings: EventEngineSettings) -> None:
        self.settings = settings
        self._shutdown = asyncio.Event()
        self._pool: asyncpg.Pool | None = None
        self._consumer: Any = None
        self._producer: Any = None
        self._emitter: EventEmitter | None = None
        self._aggregator: MetadataAggregator | None = None
        self._camera_configs: CameraConfigStore | None = None
        self._state_machines: dict[str, TrackStateMachine] = {}
        self._camera_motion: dict[str, CameraMotionState] = {}
        self._event_cooldowns: dict[tuple[str, str], float] = {}
        self._tick_task: asyncio.Task[None] | None = None
        self._started_at: float = time.time()
        self._last_tracklet_ts: float = 0.0
        self._consumer_subscribed: bool = False
        self._health_runner: Any = None

    async def start(self) -> None:
        """Initialise dependencies and start the main consumer loop."""
        self._pool = await asyncpg.create_pool(
            dsn=self.settings.db_dsn,
            min_size=2,
            max_size=10,
        )
        self._camera_configs = CameraConfigStore(
            pool=self._pool,
            default_loitering_duration_s=self.settings.loitering_duration_s,
        )

        from confluent_kafka import Consumer, Producer  # noqa: PLC0415
        from prometheus_client import start_http_server  # noqa: PLC0415

        consumer_config: dict[str, Any] = {
            "bootstrap.servers": self.settings.kafka_bootstrap,
            "group.id": self.settings.kafka_group_id,
            "auto.offset.reset": "latest",
            "enable.auto.commit": False,
        }
        producer_config: dict[str, Any] = {
            "bootstrap.servers": self.settings.kafka_bootstrap,
            "acks": "all",
            "compression.type": "zstd",
            "enable.idempotence": True,
        }
        if self.settings.kafka_security_protocol != "PLAINTEXT":
            consumer_config["security.protocol"] = self.settings.kafka_security_protocol
            producer_config["security.protocol"] = self.settings.kafka_security_protocol

        self._consumer = Consumer(consumer_config)
        self._consumer.subscribe([self.settings.kafka_input_topic])
        self._consumer_subscribed = True

        self._producer = Producer(producer_config)
        self._emitter = EventEmitter(
            pool=self._pool,
            producer=self._producer,
            output_topic=self.settings.kafka_output_topic,
        )
        self._aggregator = MetadataAggregator(pool=self._pool)

        start_http_server(self.settings.metrics_port)
        logger.info("Metrics server listening on port %d", self.settings.metrics_port)

        await self._start_health_server()

        self._tick_task = asyncio.create_task(self._tick_loop())
        await self._consume_loop()

    async def _start_health_server(self) -> None:
        try:
            from aiohttp import web  # noqa: PLC0415
        except ImportError:
            logger.warning("aiohttp not installed — /health endpoint disabled")
            return

        async def health_handler(_request: Any) -> Any:
            now = time.time()
            uptime = now - self._started_at
            checks: dict[str, str] = {}
            healthy = True

            if self._consumer_subscribed:
                checks["consumer"] = "connected"
            else:
                checks["consumer"] = "disconnected"
                healthy = False

            if self._last_tracklet_ts == 0:
                if uptime > 120:
                    checks["processing"] = "no tracklets processed after 2 minutes"
                    healthy = False
                else:
                    checks["processing"] = "warming up"
            else:
                age = now - self._last_tracklet_ts
                checks["processing"] = f"last tracklet {int(age)}s ago"
                if age > 120:
                    checks["processing"] += " (STALE)"
                    healthy = False

            checks["cooldown_s"] = str(self.settings.event_cooldown_s)
            checks["active_cooldowns"] = str(len(self._event_cooldowns))

            body = {
                "status": "ok" if healthy else "unhealthy",
                "uptime_seconds": int(uptime),
                "checks": checks,
            }
            return web.json_response(body, status=200 if healthy else 503)

        app = web.Application()
        app.router.add_get("/health", health_handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", self.settings.health_port)
        await site.start()
        self._health_runner = runner
        logger.info("Health server on port %d", self.settings.health_port)

    async def shutdown(self) -> None:
        """Flush Kafka, stop background tasks, and close connections."""
        self._shutdown.set()
        if self._tick_task is not None:
            self._tick_task.cancel()
        if self._emitter is not None:
            await self._emitter.flush()
        if self._consumer is not None:
            self._consumer_subscribed = False
            self._consumer.close()
        if self._health_runner is not None:
            try:
                await self._health_runner.cleanup()
            except Exception:
                pass
        if self._pool is not None:
            await self._pool.close()
        logger.info("Event engine shut down")

    async def _consume_loop(self) -> None:
        TrackletType = _load_tracklet_type()

        logger.info(
            "Consuming from %s (group=%s)",
            self.settings.kafka_input_topic,
            self.settings.kafka_group_id,
        )

        while not self._shutdown.is_set():
            msg = await asyncio.to_thread(
                self._consumer.poll,
                self.settings.kafka_poll_timeout_s,
            )

            if msg is None:
                continue
            if msg.error():
                logger.error("Kafka error: %s", msg.error())
                continue
            if msg.value() is None:
                await asyncio.to_thread(self._consumer.commit, asynchronous=False)
                continue

            EVENT_TRACKLETS_CONSUMED_TOTAL.inc()
            self._last_tracklet_ts = time.time()

            try:
                tracklet = TrackletType()
                tracklet.ParseFromString(msg.value())
                await self._process_tracklet(tracklet)
            except Exception:
                logger.exception(
                    "Error processing tracklet at offset %d",
                    msg.offset(),
                )

            await asyncio.to_thread(self._consumer.commit, asynchronous=False)

    async def _process_tracklet(self, tracklet: Any) -> None:
        if self._camera_configs is None or self._emitter is None:
            raise RuntimeError("service not started")

        camera_id = str(tracklet.camera_id)
        track_id = str(tracklet.track_id)
        machine = self._state_machines.get(track_id)
        if machine is None:
            machine = TrackStateMachine(
                track_id=track_id,
                camera_id=camera_id,
                object_class=OBJECT_CLASS_NAMES.get(tracklet.object_class, "unspecified"),
                camera_zones=await self._camera_configs.get(camera_id),
                stopped_threshold=self.settings.stopped_threshold,
                stopped_duration_s=self.settings.stopped_duration_s,
                stopped_resume_threshold=self.settings.stopped_resume_threshold,
                stopped_resume_duration_s=self.settings.stopped_resume_duration_s,
            )
            self._state_machines[track_id] = machine
            EVENT_ACTIVE_STATE_MACHINES.set(len(self._state_machines))

        previous_state = machine.state
        triggers = machine.update(tracklet)
        self._record_transition(previous_state.value, machine.state.value)

        await self._maybe_start_motion(tracklet)

        filtered_triggers = [t for t in triggers if not self._should_suppress(t)]
        if filtered_triggers:
            await self._emitter.emit_many(filtered_triggers)

        if getattr(tracklet, "state", None) == TRACKLET_STATE_TERMINATED:
            self._state_machines.pop(track_id, None)
            EVENT_ACTIVE_STATE_MACHINES.set(len(self._state_machines))

    async def _tick_loop(self) -> None:
        while not self._shutdown.is_set():
            await asyncio.sleep(self.settings.tick_interval_s)

            triggers: list[EventTrigger] = []
            now = time.time()

            for machine in list(self._state_machines.values()):
                previous_state = machine.state
                machine_triggers = machine.check_timers(now)
                if machine_triggers:
                    triggers.extend(machine_triggers)
                self._record_transition(previous_state.value, machine.state.value)

            await self._maybe_end_motion(now)

            filtered = [t for t in triggers if not self._should_suppress(t)]
            if filtered and self._emitter is not None:
                await self._emitter.emit_many(filtered)

            self._cleanup_cooldowns(now)

    async def _maybe_start_motion(self, tracklet: Any) -> None:
        """Open a 'motion' duration event row when a new motion burst begins."""
        if not self.settings.motion_events_enabled:
            return
        state = getattr(tracklet, "state", None)
        if state not in {TRACKLET_STATE_NEW, TRACKLET_STATE_ACTIVE}:
            return

        camera_id = str(tracklet.camera_id)
        event_time = extract_tracklet_time(tracklet)
        now = event_time.timestamp()
        timestamps = extract_event_timestamps(tracklet)
        motion_state = self._camera_motion.setdefault(camera_id, CameraMotionState())

        stillness_s = self.settings.motion_stillness_ms / 1000.0
        is_new_burst = (
            not motion_state.motion_active
            and (
                motion_state.last_activity_at is None
                or now - motion_state.last_activity_at >= stillness_s
            )
        )
        if is_new_burst:
            event_id = await self._insert_motion_event(
                camera_id=camera_id,
                start_time=event_time,
                timestamps=timestamps,
            )
            motion_state.motion_active = True
            motion_state.active_event_id = event_id
            motion_state.motion_started_at = event_time
            logger.info(
                "Motion started on %s — event %s at %s",
                camera_id, event_id, event_time.isoformat(),
            )

        motion_state.last_activity_at = now
        motion_state.last_timestamps = timestamps

    async def _maybe_end_motion(self, now: float) -> None:
        """Close any motion event whose activity has been quiet long enough."""
        if not self.settings.motion_events_enabled:
            return
        for camera_id, motion_state in self._camera_motion.items():
            if (
                not motion_state.motion_active
                or motion_state.last_activity_at is None
                or motion_state.active_event_id is None
                or motion_state.motion_started_at is None
            ):
                continue

            if now - motion_state.last_activity_at >= self.settings.motion_end_duration_s:
                end_time = datetime_from_epoch(
                    motion_state.last_activity_at + self.settings.motion_end_duration_s
                )
                event_id = motion_state.active_event_id
                start_time = motion_state.motion_started_at
                await self._close_motion_event(
                    event_id=event_id,
                    start_time=start_time,
                    end_time=end_time,
                )
                logger.info(
                    "Motion ended on %s — event %s (duration %.1fs)",
                    camera_id, event_id, (end_time - start_time).total_seconds(),
                )
                motion_state.motion_active = False
                motion_state.active_event_id = None
                motion_state.motion_started_at = None

                if self._aggregator is not None:
                    try:
                        await self._aggregator.aggregate(
                            event_id=event_id,
                            camera_id=camera_id,
                            start_time=start_time,
                            end_time=end_time,
                        )
                    except Exception:
                        logger.exception(
                            "Aggregation failed for motion event %s", event_id,
                        )

                await self._route_clip(
                    event_id=event_id,
                    camera_id=camera_id,
                    start_time=start_time,
                    end_time=end_time,
                )

    async def _route_clip(
        self,
        event_id: str,
        camera_id: str,
        start_time: datetime,
        end_time: datetime,
    ) -> None:
        """Pick clip_uri/clip_source_type based on camera recording mode.

        continuous/hybrid -> range URI resolved at play time by /clips/range.
        motion            -> synchronous POST to clip-service /extract for
                             a standalone MP4 covering pre_roll + motion + post_roll.
        """
        if self._pool is None:
            return

        async with self._pool.acquire() as conn:
            prof = await conn.fetchrow(
                """
                SELECT COALESCE(p.recording_mode, 'continuous') AS mode,
                       COALESCE(p.pre_roll_s, 5) AS pre_roll_s,
                       COALESCE(p.post_roll_s, 5) AS post_roll_s
                FROM cameras c
                LEFT JOIN camera_profiles p ON p.profile_id = c.profile_id
                WHERE c.camera_id = $1
                """,
                camera_id,
            )

        mode = prof["mode"] if prof else "continuous"
        clip_uri: str | None = None
        source_type = "segment_range"

        if mode in ("continuous", "hybrid"):
            clip_uri = build_segment_range_uri(
                camera_id=camera_id,
                start=start_time,
                end=end_time,
            )
            source_type = "segment_range"
        else:  # motion
            source_type = "standalone"
            try:
                clip_uri = await self._call_clip_service_extract(
                    event_id=event_id,
                    camera_id=camera_id,
                    motion_start=start_time,
                    motion_end=end_time,
                    pre_roll_s=float(prof["pre_roll_s"]) if prof else 5.0,
                    post_roll_s=float(prof["post_roll_s"]) if prof else 5.0,
                )
            except Exception:
                logger.exception(
                    "Standalone clip extraction failed for event %s; clip_uri stays null",
                    event_id,
                )
                clip_uri = None

        async with self._pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE events
                SET clip_uri = $1, clip_source_type = $2, updated_at = NOW()
                WHERE event_id = $3::uuid
                """,
                clip_uri, source_type, uuid.UUID(event_id),
            )
        logger.info(
            "Clip route for event %s: mode=%s source=%s uri=%s",
            event_id, mode, source_type, clip_uri,
        )

    async def _call_clip_service_extract(
        self,
        event_id: str,
        camera_id: str,
        motion_start: datetime,
        motion_end: datetime,
        pre_roll_s: float,
        post_roll_s: float,
    ) -> str | None:
        """POST to clip-service /extract and return its reported clip_uri."""
        from aiohttp import ClientSession, ClientTimeout  # noqa: PLC0415

        url = f"{self.settings.clip_service_url.rstrip('/')}/extract"
        payload = {
            "event_id": event_id,
            "camera_id": camera_id,
            "motion_start": motion_start.isoformat(),
            "motion_end": motion_end.isoformat(),
            "pre_roll_s": pre_roll_s,
            "post_roll_s": post_roll_s,
        }
        timeout = ClientTimeout(total=self.settings.clip_extract_timeout_s)
        async with ClientSession(timeout=timeout) as session:
            async with session.post(url, json=payload) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    raise RuntimeError(
                        f"clip-service /extract returned {resp.status}: {text}"
                    )
                body = await resp.json()
        return body.get("clip_uri")

    async def _insert_motion_event(
        self,
        camera_id: str,
        start_time: datetime,
        timestamps: EventTimestamps,
    ) -> str:
        if self._pool is None:
            raise RuntimeError("pool not initialised")
        event_id = str(uuid.uuid4())
        sql = """
            INSERT INTO events (
                event_id, event_type, camera_id, start_time, state,
                clip_source_type,
                source_capture_ts, edge_receive_ts, core_ingest_ts
            ) VALUES (
                $1::uuid, 'motion', $2, $3, 'active',
                'segment_range',
                $4, $5, $6
            )
        """
        async with self._pool.acquire() as conn:
            await conn.execute(
                sql,
                uuid.UUID(event_id), camera_id, start_time,
                timestamps.source_capture_ts,
                timestamps.edge_receive_ts,
                timestamps.core_ingest_ts,
            )
        return event_id

    async def _close_motion_event(
        self,
        event_id: str,
        start_time: datetime,
        end_time: datetime,
    ) -> None:
        if self._pool is None:
            raise RuntimeError("pool not initialised")
        duration_ms = max(0, int((end_time - start_time).total_seconds() * 1000))
        sql = """
            UPDATE events
            SET end_time = $2, duration_ms = $3, state = 'closed', updated_at = NOW()
            WHERE event_id = $1::uuid
        """
        async with self._pool.acquire() as conn:
            await conn.execute(sql, uuid.UUID(event_id), end_time, duration_ms)

    def _record_transition(self, from_state: str, to_state: str) -> None:
        if from_state != to_state:
            EVENT_STATE_TRANSITIONS_TOTAL.labels(
                from_state=from_state,
                to_state=to_state,
            ).inc()

    def _should_suppress(self, trigger: EventTrigger) -> bool:
        """Return True if this event should be suppressed as a duplicate.

        Only entered_scene and exited_scene are subject to cooldown.
        The cooldown is per (camera_id, event_type) — not per track.
        """
        if self.settings.event_cooldown_s <= 0:
            return False

        event_type = trigger.event_type
        if isinstance(event_type, EventType):
            event_type = event_type.value

        if event_type not in self._DEDUP_EVENT_TYPES:
            return False

        key = (trigger.camera_id, event_type)
        now = time.time()
        last = self._event_cooldowns.get(key, 0.0)

        if (now - last) < self.settings.event_cooldown_s:
            logger.debug(
                "Suppressed duplicate %s on %s (%.1fs since last)",
                event_type,
                trigger.camera_id,
                now - last,
            )
            EVENT_SUPPRESSED_TOTAL.labels(event_type=event_type).inc()
            return True

        self._event_cooldowns[key] = now
        return False

    def _cleanup_cooldowns(self, now: float) -> None:
        """Remove cooldown entries older than 2× the cooldown window."""
        if self.settings.event_cooldown_s <= 0:
            return
        stale_threshold = self.settings.event_cooldown_s * 2
        stale_keys = [
            k for k, t in self._event_cooldowns.items()
            if (now - t) > stale_threshold
        ]
        for k in stale_keys:
            del self._event_cooldowns[k]


def datetime_from_epoch(epoch_seconds: float) -> Any:
    """Return a UTC datetime from epoch seconds."""
    from datetime import datetime, timezone  # noqa: PLC0415

    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc)


async def _run() -> None:
    args = parse_args()
    settings = EventEngineSettings.from_yaml(args.config)
    setup_logging(settings.log_level)

    service = EventEngineService(settings)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(
            sig,
            lambda: asyncio.create_task(service.shutdown()),
        )

    try:
        await service.start()
    except asyncio.CancelledError:
        raise
    finally:
        await service.shutdown()


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
