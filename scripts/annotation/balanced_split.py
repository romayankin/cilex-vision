#!/usr/bin/env python3
"""Create balanced train/val/test splits with proportional site/condition representation.

Reads a unified manifest (from aggregate_datasets.py) and produces splits that
ensure each split has proportional representation from all sites and conditions,
while respecting temporal ordering within each site (reusing split_dataset.py's
chronological sequencing logic).

Usage:
    python balanced_split.py --manifest data/multi-site/unified-manifest.json \
        --output-dir data/multi-site/splits/ --ratios 70,15,15

    python balanced_split.py --manifest data/multi-site/unified-manifest.json \
        --output-dir data/multi-site/splits/ --ratios 70,15,15 --dvc --seed 42

Output:
    {output-dir}/train.json   — training split manifest
    {output-dir}/val.json     — validation split manifest
    {output-dir}/test.json    — test split manifest
    {output-dir}/split-summary.json — machine-readable summary
    {output-dir}/split-summary.md   — human-readable summary
    {output-dir}/*.dvc        — DVC tracking files (if --dvc flag)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


TIMESTAMP_KEYS: tuple[str, ...] = (
    "capture_ts",
    "source_capture_ts",
    "timestamp",
)

SPLIT_NAMES: tuple[str, ...] = ("train", "val", "test")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create balanced train/val/test splits from a unified multi-site manifest.",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        required=True,
        help="Path to unified manifest JSON from aggregate_datasets.py.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data/multi-site/splits"),
        help="Directory for output split manifests.",
    )
    parser.add_argument(
        "--ratios",
        default="70,15,15",
        help="Comma-separated train,val,test ratios (default: 70,15,15).",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Random seed for reproducible splits (default: 42).",
    )
    parser.add_argument(
        "--dvc",
        action="store_true",
        help="Write DVC-compatible .dvc tracking files.",
    )
    parser.add_argument(
        "--gap-seconds",
        type=int,
        default=300,
        help="Timestamp gap for inferring sequence boundaries (default: 300).",
    )
    return parser.parse_args()


def parse_ratios(ratios_str: str) -> tuple[float, float, float]:
    parts = [float(x.strip()) for x in ratios_str.split(",")]
    if len(parts) != 3:
        raise ValueError("ratios must have exactly 3 values: train,val,test")
    total = sum(parts)
    normalized = (parts[0] / total, parts[1] / total, parts[2] / total)
    return normalized


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
    return datetime.now(tz=UTC)


def build_stratum_key(item: dict[str, Any]) -> str:
    """Build a stratification key from site_id + object_class + condition summary."""
    site_id = item.get("site_id", "unknown")
    obj_class = item.get("object_class", "unknown")
    conditions = item.get("conditions", {})
    # Sort condition keys for deterministic key
    cond_parts = [f"{k}={v}" for k, v in sorted(conditions.items())] if conditions else ["no-conditions"]
    return f"{site_id}|{obj_class}|{','.join(cond_parts)}"


# ---------------------------------------------------------------------------
# Sequence grouping (reuses split_dataset.py logic)
# ---------------------------------------------------------------------------


def group_into_sequences(
    items: list[dict[str, Any]],
    gap_seconds: int,
) -> list[list[dict[str, Any]]]:
    """Group items into temporal sequences per camera, maintaining temporal order."""
    # Sort by timestamp within each camera
    items_sorted = sorted(items, key=lambda i: (i.get("camera_id", ""), pick_timestamp(i)))

    sequences: list[list[dict[str, Any]]] = []
    current_seq: list[dict[str, Any]] = []
    last_camera: str | None = None
    last_ts: datetime | None = None

    for item in items_sorted:
        camera_id = item.get("camera_id", "")
        ts = pick_timestamp(item)

        needs_new_seq = (
            camera_id != last_camera
            or last_ts is None
            or (ts - last_ts).total_seconds() > gap_seconds
        )

        if needs_new_seq and current_seq:
            sequences.append(current_seq)
            current_seq = []

        current_seq.append(item)
        last_camera = camera_id
        last_ts = ts

    if current_seq:
        sequences.append(current_seq)

    return sequences


# ---------------------------------------------------------------------------
# Balanced splitting via stratified allocation
# ---------------------------------------------------------------------------


def allocate_balanced(
    items: list[dict[str, Any]],
    ratios: tuple[float, float, float],
    seed: int,
    gap_seconds: int,
) -> dict[str, list[dict[str, Any]]]:
    """Split items with proportional site/condition/class representation.

    Strategy:
    1. Group items by stratum (site + class + conditions)
    2. Within each stratum, group into temporal sequences
    3. Allocate sequences to splits proportionally, respecting temporal order
    """
    rng = random.Random(seed)
    train_r, val_r, test_r = ratios

    # Group by stratum
    strata: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in items:
        key = build_stratum_key(item)
        strata[key] = strata.get(key, [])
        strata[key].append(item)

    splits: dict[str, list[dict[str, Any]]] = {"train": [], "val": [], "test": []}

    for stratum_key in sorted(strata.keys()):
        stratum_items = strata[stratum_key]
        sequences = group_into_sequences(stratum_items, gap_seconds)

        if not sequences:
            continue

        # Sort sequences by earliest timestamp (temporal ordering)
        sequences.sort(key=lambda seq: pick_timestamp(seq[0]))

        total = sum(len(seq) for seq in sequences)
        train_target = total * train_r
        val_target = total * (train_r + val_r)

        running = 0
        current_split = "train"

        for seq in sequences:
            projected = running + len(seq)

            if current_split == "train" and splits["train"] and projected > train_target and len(sequences) >= 3:
                current_split = "val"
            elif current_split == "val" and splits["val"] and projected > val_target:
                current_split = "test"

            splits[current_split].extend(seq)
            running = projected

    # If any split is empty due to small strata, redistribute
    _rebalance_if_empty(splits, rng)

    return splits


def _rebalance_if_empty(
    splits: dict[str, list[dict[str, Any]]],
    rng: random.Random,
) -> None:
    """Ensure no split is completely empty by redistributing from largest."""
    for split_name in SPLIT_NAMES:
        if not splits[split_name]:
            # Find the largest split and move some items
            largest = max(SPLIT_NAMES, key=lambda s: len(splits[s]))
            if len(splits[largest]) >= 3:
                # Take ~10% from largest for the empty split
                donor = splits[largest]
                rng.shuffle(donor)
                take = max(1, len(donor) // 10)
                splits[split_name] = donor[:take]
                splits[largest] = donor[take:]


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def build_summary(
    splits: dict[str, list[dict[str, Any]]],
    ratios: tuple[float, float, float],
    seed: int,
    manifest_path: str,
) -> dict[str, Any]:
    """Build machine-readable summary of splits."""
    total = sum(len(items) for items in splits.values())

    summary: dict[str, Any] = {
        "source_manifest": manifest_path,
        "seed": seed,
        "ratios": {"train": ratios[0], "val": ratios[1], "test": ratios[2]},
        "total_items": total,
        "splits": {},
    }

    for split_name in SPLIT_NAMES:
        items = splits[split_name]
        sites = sorted({i.get("site_id", "unknown") for i in items})
        classes = sorted({i.get("object_class", "unknown") for i in items})
        cameras = sorted({i.get("camera_id", "unknown") for i in items})
        timestamps = [pick_timestamp(i) for i in items]

        # Per-site counts
        site_counts: dict[str, int] = defaultdict(int)
        class_counts: dict[str, int] = defaultdict(int)
        for item in items:
            site_counts[item.get("site_id", "unknown")] += 1
            class_counts[item.get("object_class", "unknown")] += 1

        summary["splits"][split_name] = {
            "item_count": len(items),
            "proportion": len(items) / total if total else 0,
            "sites": sites,
            "site_counts": dict(site_counts),
            "classes": classes,
            "class_counts": dict(class_counts),
            "cameras": cameras,
            "camera_count": len(cameras),
            "time_start": min(timestamps).isoformat() if timestamps else None,
            "time_end": max(timestamps).isoformat() if timestamps else None,
        }

    return summary


def write_split_manifests(
    output_dir: Path,
    splits: dict[str, list[dict[str, Any]]],
) -> None:
    """Write train.json, val.json, test.json split manifests."""
    output_dir.mkdir(parents=True, exist_ok=True)

    for split_name in SPLIT_NAMES:
        payload = {
            "split": split_name,
            "items": splits[split_name],
        }
        (output_dir / f"{split_name}.json").write_text(
            json.dumps(payload, indent=2) + "\n",
            encoding="utf-8",
        )


def write_summary(output_dir: Path, summary: dict[str, Any]) -> None:
    """Write split-summary.json and split-summary.md."""
    # JSON summary
    (output_dir / "split-summary.json").write_text(
        json.dumps(summary, indent=2) + "\n",
        encoding="utf-8",
    )

    # Markdown summary
    lines = [
        "# Multi-Site Dataset Split Summary",
        "",
        f"Generated: {datetime.now(tz=UTC).strftime('%Y-%m-%d %H:%M UTC')}",
        f"Source: `{summary['source_manifest']}`",
        f"Seed: {summary['seed']}",
        "",
        "## Split Overview",
        "",
        "| Split | Items | Proportion | Sites | Cameras | Start | End |",
        "|-------|------:|------------|------:|--------:|-------|-----|",
    ]

    for split_name in SPLIT_NAMES:
        split = summary["splits"][split_name]
        lines.append(
            f"| {split_name} | {split['item_count']:,} | "
            f"{split['proportion']:.1%} | {len(split['sites'])} | "
            f"{split['camera_count']} | "
            f"{split['time_start'] or 'n/a'} | {split['time_end'] or 'n/a'} |"
        )

    lines.append("")
    lines.append("## Per-Site Distribution")
    lines.append("")

    # Collect all sites
    all_sites = sorted({
        s for split in summary["splits"].values() for s in split.get("sites", [])
    })
    header = "| Site | " + " | ".join(SPLIT_NAMES) + " | Total |"
    sep = "|------|" + "|".join(["------:" for _ in SPLIT_NAMES]) + "|------:|"
    lines.append(header)
    lines.append(sep)
    for site in all_sites:
        counts = [summary["splits"][s]["site_counts"].get(site, 0) for s in SPLIT_NAMES]
        total = sum(counts)
        row = f"| {site} | " + " | ".join(f"{c:,}" for c in counts) + f" | {total:,} |"
        lines.append(row)

    lines.append("")
    lines.append("## Per-Class Distribution")
    lines.append("")

    all_classes = sorted({
        c for split in summary["splits"].values() for c in split.get("classes", [])
    })
    header = "| Class | " + " | ".join(SPLIT_NAMES) + " | Total |"
    sep = "|-------|" + "|".join(["------:" for _ in SPLIT_NAMES]) + "|------:|"
    lines.append(header)
    lines.append(sep)
    for cls in all_classes:
        counts = [summary["splits"][s]["class_counts"].get(cls, 0) for s in SPLIT_NAMES]
        total = sum(counts)
        row = f"| {cls} | " + " | ".join(f"{c:,}" for c in counts) + f" | {total:,} |"
        lines.append(row)

    lines.append("")

    (output_dir / "split-summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_dvc_files(output_dir: Path) -> None:
    """Write DVC-compatible .dvc tracking files for each split manifest."""
    for split_name in SPLIT_NAMES:
        json_path = output_dir / f"{split_name}.json"
        if not json_path.exists():
            continue

        content = json_path.read_bytes()
        md5 = hashlib.md5(content).hexdigest()  # noqa: S324
        size = len(content)

        dvc_content = {
            "outs": [
                {
                    "md5": md5,
                    "size": size,
                    "path": f"{split_name}.json",
                }
            ],
        }

        dvc_path = output_dir / f"{split_name}.json.dvc"
        dvc_path.write_text(
            json.dumps(dvc_content, indent=2) + "\n",
            encoding="utf-8",
        )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    args = parse_args()
    ratios = parse_ratios(args.ratios)

    if not args.manifest.exists():
        raise FileNotFoundError(f"manifest not found: {args.manifest}")

    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    items = manifest.get("items", [])
    if not items:
        raise ValueError("manifest contains no items")

    print(f"Splitting {len(items)} items with ratios {args.ratios} (seed={args.seed})...")

    splits = allocate_balanced(items, ratios, args.seed, args.gap_seconds)

    # Write outputs
    write_split_manifests(args.output_dir, splits)
    summary = build_summary(splits, ratios, args.seed, str(args.manifest))
    write_summary(args.output_dir, summary)

    if args.dvc:
        write_dvc_files(args.output_dir)
        print("DVC tracking files written.")

    # Print summary
    print(f"\nSplit output: {args.output_dir}/")
    for split_name in SPLIT_NAMES:
        split = summary["splits"][split_name]
        print(f"  {split_name}: {split['item_count']:,} items ({split['proportion']:.1%}) "
              f"from {len(split['sites'])} sites")

    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        raise SystemExit(str(exc)) from exc
