#!/usr/bin/env python3
"""Split an evaluation or training manifest into train/val/test with time separation.

Expected manifest shape:
{
  "items": [
    {
      "item_id": "cam-01:clip-003:frame-0042",
      "camera_id": "cam-01",
      "capture_ts": "2026-04-06T10:15:00Z",
      "sequence_id": "clip-003",
      "source_uri": "s3://datasets/pilot/clip-003/frame-0042.jpg"
    }
  ]
}

If sequence_id is absent, the script derives contiguous sequences per camera
using timestamp gaps so frames from the same temporal window are never split
across train / val / test.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


TIMESTAMP_KEYS: tuple[str, ...] = (
    "capture_ts",
    "source_capture_ts",
    "timestamp",
    "edge_receive_ts",
    "core_ingest_ts",
)
EXPLICIT_SEQUENCE_KEYS: tuple[str, ...] = (
    "sequence_id",
    "clip_id",
    "video_id",
    "task_id",
)


@dataclass(frozen=True)
class DatasetItem:
    item_id: str
    camera_id: str
    timestamp: datetime
    payload: dict[str, Any]
    explicit_sequence_id: str | None


@dataclass
class SequenceGroup:
    sequence_id: str
    camera_ids: set[str] = field(default_factory=set)
    items: list[DatasetItem] = field(default_factory=list)

    @property
    def start_ts(self) -> datetime:
        return self.items[0].timestamp

    @property
    def end_ts(self) -> datetime:
        return self.items[-1].timestamp


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "manifest",
        type=Path,
        help="Input manifest JSON with a top-level items array.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data/splits"),
        help="Directory that will receive split manifests and summaries.",
    )
    parser.add_argument(
        "--train-ratio",
        type=float,
        default=0.70,
        help="Target proportion for the train split.",
    )
    parser.add_argument(
        "--val-ratio",
        type=float,
        default=0.15,
        help="Target proportion for the validation split.",
    )
    parser.add_argument(
        "--test-ratio",
        type=float,
        default=0.15,
        help="Target proportion for the test split.",
    )
    parser.add_argument(
        "--gap-seconds",
        type=int,
        default=300,
        help="Timestamp gap used to infer sequence boundaries when sequence_id is absent.",
    )
    return parser.parse_args()


def parse_timestamp(value: str) -> datetime:
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def pick_timestamp(item: dict[str, Any]) -> datetime:
    for key in TIMESTAMP_KEYS:
        value = item.get(key)
        if value:
            return parse_timestamp(str(value))
    raise ValueError(f"item {item.get('item_id')!r} is missing a supported timestamp field")


def load_items(path: Path) -> list[DatasetItem]:
    if not path.exists():
        raise FileNotFoundError(f"manifest not found: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    items_raw = payload.get("items")
    if not isinstance(items_raw, list) or not items_raw:
        raise ValueError("manifest must contain a non-empty top-level items array")

    items: list[DatasetItem] = []
    for item in items_raw:
        item_id = item.get("item_id")
        if not item_id:
            raise ValueError("every manifest item requires item_id")
        camera_id = str(item.get("camera_id") or "unknown-camera")
        explicit_sequence_id = None
        for key in EXPLICIT_SEQUENCE_KEYS:
            if item.get(key):
                explicit_sequence_id = str(item[key])
                break
        items.append(
            DatasetItem(
                item_id=str(item_id),
                camera_id=camera_id,
                timestamp=pick_timestamp(item),
                payload=dict(item),
                explicit_sequence_id=explicit_sequence_id,
            )
        )

    items.sort(key=lambda item: (item.timestamp, item.camera_id, item.item_id))
    return items


def group_sequences(items: list[DatasetItem], gap_seconds: int) -> list[SequenceGroup]:
    groups_by_id: dict[str, SequenceGroup] = {}
    inferred_counter_by_camera: dict[str, int] = {}
    last_timestamp_by_camera: dict[str, datetime] = {}
    current_inferred_key_by_camera: dict[str, str] = {}

    for item in items:
        if item.explicit_sequence_id:
            sequence_id = item.explicit_sequence_id
        else:
            previous_timestamp = last_timestamp_by_camera.get(item.camera_id)
            needs_new_group = (
                previous_timestamp is None
                or (item.timestamp - previous_timestamp).total_seconds() > gap_seconds
            )
            if needs_new_group:
                inferred_counter_by_camera[item.camera_id] = inferred_counter_by_camera.get(item.camera_id, 0) + 1
                current_inferred_key_by_camera[item.camera_id] = (
                    f"{item.camera_id}:inferred-{inferred_counter_by_camera[item.camera_id]:04d}"
                )
            sequence_id = current_inferred_key_by_camera[item.camera_id]
            last_timestamp_by_camera[item.camera_id] = item.timestamp

        group = groups_by_id.setdefault(sequence_id, SequenceGroup(sequence_id=sequence_id))
        group.camera_ids.add(item.camera_id)
        group.items.append(item)

    groups = sorted(groups_by_id.values(), key=lambda group: (group.start_ts, group.sequence_id))
    for group in groups:
        group.items.sort(key=lambda item: (item.timestamp, item.camera_id, item.item_id))
    return groups


def assign_splits(
    groups: list[SequenceGroup],
    train_ratio: float,
    val_ratio: float,
    test_ratio: float,
) -> dict[str, list[SequenceGroup]]:
    if not groups:
        raise ValueError("cannot split an empty group list")
    ratio_sum = train_ratio + val_ratio + test_ratio
    if abs(ratio_sum - 1.0) > 1e-9:
        raise ValueError(f"split ratios must sum to 1.0, got {ratio_sum}")

    total_items = sum(len(group.items) for group in groups)
    train_target = total_items * train_ratio
    val_target = total_items * (train_ratio + val_ratio)
    assignments: dict[str, list[SequenceGroup]] = {"train": [], "val": [], "test": []}

    running_items = 0
    current_split = "train"
    for index, group in enumerate(groups):
        remaining_groups = len(groups) - index
        projected_items = running_items + len(group.items)
        if (
            current_split == "train"
            and assignments["train"]
            and projected_items > train_target
            and remaining_groups >= 2
        ):
            current_split = "val"
        elif (
            current_split == "val"
            and assignments["val"]
            and projected_items > val_target
            and remaining_groups >= 1
        ):
            current_split = "test"

        assignments[current_split].append(group)
        running_items = projected_items

    rebalance_if_needed(assignments)
    return assignments


def rebalance_if_needed(assignments: dict[str, list[SequenceGroup]]) -> None:
    if assignments["train"] and assignments["val"] and assignments["test"]:
        return

    all_groups = assignments["train"] + assignments["val"] + assignments["test"]
    if len(all_groups) < 3:
        return

    ordered = sorted(all_groups, key=lambda group: (group.start_ts, group.sequence_id))
    assignments["train"] = [ordered[0]]
    assignments["val"] = [ordered[1]]
    assignments["test"] = ordered[2:]


def flatten(groups: list[SequenceGroup]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for group in groups:
        for item in group.items:
            payload = dict(item.payload)
            payload["sequence_id"] = payload.get("sequence_id", group.sequence_id)
            items.append(payload)
    return items


def build_summary(assignments: dict[str, list[SequenceGroup]]) -> dict[str, Any]:
    summary: dict[str, Any] = {"splits": {}}
    for split_name, groups in assignments.items():
        items = flatten(groups)
        timestamps = [item.timestamp for group in groups for item in group.items]
        summary["splits"][split_name] = {
            "sequence_count": len(groups),
            "item_count": len(items),
            "camera_ids": sorted({camera_id for group in groups for camera_id in group.camera_ids}),
            "time_start": min(timestamps).isoformat() if timestamps else None,
            "time_end": max(timestamps).isoformat() if timestamps else None,
        }
    return summary


def write_outputs(output_dir: Path, assignments: dict[str, list[SequenceGroup]], summary: dict[str, Any]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)

    for split_name, groups in assignments.items():
        payload = {
            "split": split_name,
            "items": flatten(groups),
        }
        (output_dir / f"{split_name}.json").write_text(
            json.dumps(payload, indent=2) + "\n",
            encoding="utf-8",
        )

    (output_dir / "split-summary.json").write_text(
        json.dumps(summary, indent=2) + "\n",
        encoding="utf-8",
    )

    markdown_lines = [
        "# Dataset Split Summary",
        "",
        "| Split | Sequences | Items | Start | End | Cameras |",
        "|-------|-----------|-------|-------|-----|---------|",
    ]
    for split_name in ("train", "val", "test"):
        split = summary["splits"][split_name]
        markdown_lines.append(
            f"| {split_name} | {split['sequence_count']} | {split['item_count']} | "
            f"{split['time_start'] or 'n/a'} | {split['time_end'] or 'n/a'} | "
            f"{', '.join(split['camera_ids']) or 'n/a'} |"
        )

    (output_dir / "split-summary.md").write_text(
        "\n".join(markdown_lines) + "\n",
        encoding="utf-8",
    )


def main() -> None:
    args = parse_args()
    items = load_items(args.manifest)
    groups = group_sequences(items, args.gap_seconds)
    assignments = assign_splits(groups, args.train_ratio, args.val_ratio, args.test_ratio)
    summary = build_summary(assignments)
    summary["source_manifest"] = str(args.manifest)
    summary["gap_seconds"] = args.gap_seconds
    summary["ratios"] = {
        "train": args.train_ratio,
        "val": args.val_ratio,
        "test": args.test_ratio,
    }
    write_outputs(args.output_dir, assignments, summary)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        raise SystemExit(str(exc)) from exc
