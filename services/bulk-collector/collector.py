"""Age / size based batching for the Metadata Bulk Collector."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

from metrics import ROWS_STAGED
from writer import DetectionRow, TrackObservationRow


@dataclass(frozen=True)
class KafkaOffsetCommit:
    """One offset that becomes committable after a batch flush succeeds."""

    group_id: str
    topic: str
    partition: int
    offset: int


@dataclass(frozen=True)
class BufferedMessage:
    """Rows derived from one Kafka message plus its commit token."""

    commit: KafkaOffsetCommit
    detection_rows: list[DetectionRow]
    track_observation_rows: list[TrackObservationRow]


@dataclass(frozen=True)
class FlushBatch:
    """A flush-ready batch of buffered messages."""

    messages: list[BufferedMessage]
    oldest_row_monotonic: float

    @property
    def detection_rows(self) -> list[DetectionRow]:
        rows: list[DetectionRow] = []
        for message in self.messages:
            rows.extend(message.detection_rows)
        return rows

    @property
    def track_observation_rows(self) -> list[TrackObservationRow]:
        rows: list[TrackObservationRow] = []
        for message in self.messages:
            rows.extend(message.track_observation_rows)
        return rows


class BatchCollector:
    """Collects Kafka-derived rows and flushes by message count or age."""

    def __init__(
        self,
        *,
        batch_size: int = 1000,
        max_age_ms: int = 500,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.batch_size = batch_size
        self.max_age_s = max_age_ms / 1000.0
        self.clock = clock or time.monotonic
        self._messages: list[BufferedMessage] = []
        self._oldest_row_monotonic: float | None = None
        self._refresh_metrics()

    def add(self, message: BufferedMessage) -> FlushBatch | None:
        """Stage one parsed Kafka message and flush if the batch is full."""
        now = float(self.clock())
        if self._oldest_row_monotonic is None:
            self._oldest_row_monotonic = now
        self._messages.append(message)
        self._refresh_metrics()
        if len(self._messages) >= self.batch_size:
            return self._pop_ready()
        return None

    def flush_due(self) -> FlushBatch | None:
        """Flush the current batch when its age threshold has elapsed."""
        if not self._messages or self._oldest_row_monotonic is None:
            return None
        now = float(self.clock())
        if now - self._oldest_row_monotonic < self.max_age_s:
            return None
        return self._pop_ready()

    def flush_all(self) -> FlushBatch | None:
        """Flush every staged message immediately."""
        if not self._messages:
            return None
        return self._pop_ready()

    def requeue(self, batch: FlushBatch) -> None:
        """Restore a failed batch to the front of the buffer."""
        self._messages = list(batch.messages) + self._messages
        if self._oldest_row_monotonic is None:
            self._oldest_row_monotonic = batch.oldest_row_monotonic
        else:
            self._oldest_row_monotonic = min(self._oldest_row_monotonic, batch.oldest_row_monotonic)
        self._refresh_metrics()

    def staged_messages(self) -> int:
        """Current buffered message count."""
        return len(self._messages)

    def _pop_ready(self) -> FlushBatch:
        if self._oldest_row_monotonic is None:
            raise RuntimeError("buffer age missing for non-empty batch")
        batch = FlushBatch(
            messages=self._messages,
            oldest_row_monotonic=self._oldest_row_monotonic,
        )
        self._messages = []
        self._oldest_row_monotonic = None
        self._refresh_metrics()
        return batch

    def _refresh_metrics(self) -> None:
        detection_rows = 0
        track_rows = 0
        for message in self._messages:
            detection_rows += len(message.detection_rows)
            track_rows += len(message.track_observation_rows)
        ROWS_STAGED.labels(table="detections").set(detection_rows)
        ROWS_STAGED.labels(table="track_observations").set(track_rows)

