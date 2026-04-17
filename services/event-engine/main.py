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
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import asyncpg

from config import EventEngineSettings
from event_emitter import EventEmitter
from metrics import (
    EVENT_ACTIVE_STATE_MACHINES,
    EVENT_STATE_TRANSITIONS_TOTAL,
    EVENT_SUPPRESSED_TOTAL,
    EVENT_TRACKLETS_CONSUMED_TOTAL,
)
from state_machine import (
    CameraZones,
    EventTrigger,
    EventType,
    TrackStateMachine,
    extract_event_timestamps,
    extract_tracklet_time,
    make_point_event,
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
    """Best-effort camera activity derived from tracklet flow."""

    motion_active: bool = False
    last_activity_at: float | None = None
    last_timestamps: Any = None


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

        motion_triggers = self._update_camera_motion(tracklet)
        if motion_triggers:
            triggers.extend(motion_triggers)

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

            triggers.extend(self._check_camera_motion(now))
            filtered = [t for t in triggers if not self._should_suppress(t)]
            if filtered and self._emitter is not None:
                await self._emitter.emit_many(filtered)

            self._cleanup_cooldowns(now)

    def _update_camera_motion(self, tracklet: Any) -> list[EventTrigger]:
        if not self.settings.motion_events_enabled:
            return []
        state = getattr(tracklet, "state", None)
        if state not in {TRACKLET_STATE_NEW, TRACKLET_STATE_ACTIVE}:
            return []

        camera_id = str(tracklet.camera_id)
        event_time = extract_tracklet_time(tracklet)
        now = event_time.timestamp()
        timestamps = extract_event_timestamps(tracklet)
        motion_state = self._camera_motion.setdefault(camera_id, CameraMotionState())
        triggers: list[EventTrigger] = []

        stillness_s = self.settings.motion_stillness_ms / 1000.0
        if (
            not motion_state.motion_active
            and (
                motion_state.last_activity_at is None
                or now - motion_state.last_activity_at >= stillness_s
            )
        ):
            triggers.append(
                make_point_event(
                    event_type=EventType.MOTION_STARTED,
                    camera_id=camera_id,
                    track_id=None,
                    event_time=event_time,
                    timestamps=timestamps,
                )
            )
            motion_state.motion_active = True

        motion_state.last_activity_at = now
        motion_state.last_timestamps = timestamps
        return triggers

    def _check_camera_motion(self, now: float) -> list[EventTrigger]:
        if not self.settings.motion_events_enabled:
            return []
        triggers: list[EventTrigger] = []
        for camera_id, motion_state in self._camera_motion.items():
            if (
                not motion_state.motion_active
                or motion_state.last_activity_at is None
                or motion_state.last_timestamps is None
            ):
                continue

            if now - motion_state.last_activity_at >= self.settings.motion_end_duration_s:
                event_time = datetime_from_epoch(
                    motion_state.last_activity_at + self.settings.motion_end_duration_s
                )
                triggers.append(
                    make_point_event(
                        event_type=EventType.MOTION_ENDED,
                        camera_id=camera_id,
                        track_id=None,
                        event_time=event_time,
                        timestamps=motion_state.last_timestamps,
                    )
                )
                motion_state.motion_active = False
        return triggers

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
