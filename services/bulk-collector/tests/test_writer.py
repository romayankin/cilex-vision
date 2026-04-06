"""COPY-writer unit tests."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest

from writer import AsyncpgBulkWriter, DetectionRow, TrackObservationRow


class FakeClock:
    """Mutable monotonic clock for dedup TTL tests."""

    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


class FakeTransaction:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


class FakeConnection:
    """Captures COPY calls instead of talking to PostgreSQL."""

    def __init__(self) -> None:
        self.copy_calls: list[tuple[str, list[tuple[object, ...]], list[str]]] = []
        self.executed: list[str] = []

    def transaction(self) -> FakeTransaction:
        return FakeTransaction()

    async def copy_records_to_table(
        self,
        table_name: str,
        *,
        records: list[tuple[object, ...]],
        columns: list[str],
    ) -> None:
        self.copy_calls.append((table_name, records, columns))

    async def execute(self, sql: str) -> None:
        self.executed.append(sql)


class FakeAcquire:
    def __init__(self, conn: FakeConnection) -> None:
        self.conn = conn

    async def __aenter__(self) -> FakeConnection:
        return self.conn

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None


class FakePool:
    """Minimal asyncpg pool fake."""

    def __init__(self) -> None:
        self.conn = FakeConnection()
        self.closed = False

    def acquire(self) -> FakeAcquire:
        return FakeAcquire(self.conn)

    async def close(self) -> None:
        self.closed = True


def make_detection_row(frame_seq: int, track_id=None) -> DetectionRow:
    now = datetime(2026, 4, 6, tzinfo=timezone.utc)
    return DetectionRow(
        time=now,
        camera_id="cam-01",
        frame_seq=frame_seq,
        object_class="person",
        confidence=0.92,
        bbox_x=0.1,
        bbox_y=0.2,
        bbox_w=0.3,
        bbox_h=0.4,
        local_track_id=track_id,
        model_version="1.0.0",
    )


def make_track_row(frame_seq: int, track_id) -> TrackObservationRow:
    now = datetime(2026, 4, 6, tzinfo=timezone.utc)
    return TrackObservationRow(
        time=now,
        camera_id="cam-01",
        frame_seq=frame_seq,
        local_track_id=track_id,
        centroid_x=0.25,
        centroid_y=0.4,
        bbox_area=0.12,
        embedding_ref="minio://embeddings/track.bin",
    )


@pytest.mark.asyncio
async def test_writer_uses_copy_for_detections_and_dedupes() -> None:
    pool = FakePool()
    clock = FakeClock()
    writer = AsyncpgBulkWriter(
        dsn="postgresql://unused",
        pool=pool,
        dedup_ttl_s=60,
        dedup_max_keys=100,
        clock=clock,
    )

    track_id = uuid4()
    row = make_detection_row(7, track_id)
    result = await writer.write_detection_rows([row, row])

    assert result.rows_written == 1
    assert result.duplicates_skipped == 1
    assert len(pool.conn.copy_calls) == 1
    table_name, records, columns = pool.conn.copy_calls[0]
    assert table_name == "detections"
    assert len(records) == 1
    assert columns[0] == "time"
    assert columns[-1] == "model_version"


@pytest.mark.asyncio
async def test_writer_respects_dedup_ttl_for_track_observations() -> None:
    pool = FakePool()
    clock = FakeClock()
    writer = AsyncpgBulkWriter(
        dsn="postgresql://unused",
        pool=pool,
        dedup_ttl_s=1,
        dedup_max_keys=100,
        clock=clock,
    )

    track_id = uuid4()
    row = make_track_row(11, track_id)

    first = await writer.write_track_observation_rows([row])
    assert first.rows_written == 1

    second = await writer.write_track_observation_rows([row])
    assert second.rows_written == 0
    assert second.duplicates_skipped == 1

    clock.value = 2.0
    third = await writer.write_track_observation_rows([row])
    assert third.rows_written == 1
    assert len(pool.conn.copy_calls) == 2
    assert pool.conn.copy_calls[-1][0] == "track_observations"

