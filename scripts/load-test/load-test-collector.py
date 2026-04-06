#!/usr/bin/env python3
"""Synthetic load test for the Metadata Bulk Collector batching path."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid5

SERVICE_ROOT = Path(__file__).resolve().parents[2] / "services" / "bulk-collector"
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

from collector import BatchCollector, BufferedMessage, KafkaOffsetCommit
from writer import DetectionRow, TrackObservationRow, WriteResult


NAMESPACE = UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")


@dataclass
class CountingWriter:
    """In-memory writer used to validate lossless batching."""

    detections_written: int = 0
    track_rows_written: int = 0

    async def write_detection_rows(self, rows: list[DetectionRow]) -> WriteResult:
        self.detections_written += len(rows)
        return WriteResult("detections", len(rows), 0, 0.0)

    async def write_track_observation_rows(
        self,
        rows: list[TrackObservationRow],
    ) -> WriteResult:
        self.track_rows_written += len(rows)
        return WriteResult("track_observations", len(rows), 0, 0.0)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cameras", type=int, default=100)
    parser.add_argument("--rate-per-camera", type=int, default=50)
    parser.add_argument("--duration-s", type=int, default=300)
    parser.add_argument("--batch-size", type=int, default=1000)
    parser.add_argument("--max-age-ms", type=int, default=500)
    parser.add_argument("--track-cardinality", type=int, default=20)
    return parser.parse_args()


def make_message(
    camera_index: int,
    frame_seq: int,
    message_index: int,
    *,
    track_cardinality: int,
) -> BufferedMessage:
    camera_id = f"cam-{camera_index:03d}"
    track_id = uuid5(NAMESPACE, f"{camera_id}:{frame_seq % track_cardinality}")
    now = datetime(2026, 4, 6, tzinfo=timezone.utc) + timedelta(milliseconds=message_index)
    detection_row = DetectionRow(
        time=now,
        camera_id=camera_id,
        frame_seq=frame_seq,
        object_class="person",
        confidence=0.90,
        bbox_x=0.1,
        bbox_y=0.2,
        bbox_w=0.3,
        bbox_h=0.4,
        local_track_id=track_id,
        model_version="synthetic-1.0.0",
    )
    track_row = TrackObservationRow(
        time=now,
        camera_id=camera_id,
        frame_seq=frame_seq,
        local_track_id=track_id,
        centroid_x=0.25,
        centroid_y=0.40,
        bbox_area=0.12,
        embedding_ref=None,
    )
    return BufferedMessage(
        commit=KafkaOffsetCommit(
            group_id="bulk-collector-detections",
            topic="bulk.detections",
            partition=camera_index % 12,
            offset=message_index,
        ),
        detection_rows=[detection_row],
        track_observation_rows=[track_row],
    )


async def main() -> None:
    args = parse_args()
    collector = BatchCollector(batch_size=args.batch_size, max_age_ms=args.max_age_ms)
    writer = CountingWriter()
    total_messages = args.cameras * args.rate_per_camera * args.duration_s
    started = time.monotonic()
    generated = 0

    for second in range(args.duration_s):
        for camera_index in range(args.cameras):
            for offset in range(args.rate_per_camera):
                frame_seq = (second * args.rate_per_camera) + offset + 1
                message = make_message(
                    camera_index,
                    frame_seq,
                    generated,
                    track_cardinality=args.track_cardinality,
                )
                generated += 1
                ready = collector.add(message)
                if ready is not None:
                    await writer.write_detection_rows(ready.detection_rows)
                    await writer.write_track_observation_rows(ready.track_observation_rows)

    final_batch = collector.flush_all()
    if final_batch is not None:
        await writer.write_detection_rows(final_batch.detection_rows)
        await writer.write_track_observation_rows(final_batch.track_observation_rows)

    elapsed_s = time.monotonic() - started
    if writer.detections_written != total_messages:
        raise SystemExit(
            f"data loss detected: generated={total_messages} written={writer.detections_written}"
        )
    if writer.track_rows_written != total_messages:
        raise SystemExit(
            "track observation loss detected: "
            f"generated={total_messages} written={writer.track_rows_written}"
        )

    summary = {
        "messages_generated": total_messages,
        "detections_written": writer.detections_written,
        "track_rows_written": writer.track_rows_written,
        "elapsed_s": round(elapsed_s, 3),
        "effective_rows_per_sec": round((writer.detections_written + writer.track_rows_written) / max(elapsed_s, 0.001), 2),
    }
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    asyncio.run(main())
