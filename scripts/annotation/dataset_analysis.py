#!/usr/bin/env python3
"""Compute dataset statistics and identify coverage gaps across sites.

Reads a unified manifest (from aggregate_datasets.py) and generates:
- Per-class counts (total, per-site, per-condition)
- Condition distribution (lighting, weather, camera_model)
- Camera coverage (cameras per site, samples per camera)
- Gap analysis (classes with too few samples at any site)
- Markdown coverage report with tables

Usage:
    python dataset_analysis.py --manifest data/multi-site/unified-manifest.json \
        --output docs/dataset-coverage-report.md

    python dataset_analysis.py --manifest data/multi-site/unified-manifest.json \
        --output docs/dataset-coverage-report.md --min-samples 200
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# Canonical classes from services/db/models.py
CANONICAL_CLASSES: tuple[str, ...] = (
    "person",
    "car",
    "truck",
    "bus",
    "bicycle",
    "motorcycle",
    "animal",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compute dataset statistics and generate coverage report.",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        required=True,
        help="Path to unified manifest JSON from aggregate_datasets.py.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("docs/dataset-coverage-report.md"),
        help="Output path for Markdown coverage report.",
    )
    parser.add_argument(
        "--output-json",
        type=Path,
        help="Optional path for machine-readable statistics JSON.",
    )
    parser.add_argument(
        "--min-samples",
        type=int,
        default=100,
        help="Minimum samples per class at any site to avoid a gap flag (default: 100).",
    )
    return parser.parse_args()


def load_manifest(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"manifest not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------


def compute_stats(items: list[dict[str, Any]], min_samples: int) -> dict[str, Any]:
    """Compute all dataset statistics from manifest items."""
    sites: set[str] = set()
    cameras: set[str] = set()
    cameras_per_site: dict[str, set[str]] = defaultdict(set)

    class_total: Counter[str] = Counter()
    class_per_site: dict[str, Counter[str]] = defaultdict(Counter)
    class_per_condition: dict[str, Counter[str]] = defaultdict(Counter)

    condition_counts: dict[str, Counter[str]] = defaultdict(Counter)
    camera_model_counts: Counter[str] = Counter()
    samples_per_camera: Counter[str] = Counter()
    samples_per_site: Counter[str] = Counter()
    project_counts: Counter[str] = Counter()

    for item in items:
        site_id = item.get("site_id", "unknown")
        camera_id = item.get("camera_id", "unknown")
        obj_class = item.get("object_class", "unknown")
        conditions = item.get("conditions", {})
        camera_model = item.get("camera_model")

        sites.add(site_id)
        cameras.add(camera_id)
        cameras_per_site[site_id].add(camera_id)

        class_total[obj_class] += 1
        class_per_site[site_id][obj_class] += 1
        samples_per_camera[camera_id] += 1
        samples_per_site[site_id] += 1

        if item.get("project_name"):
            project_counts[item["project_name"]] += 1

        if camera_model:
            camera_model_counts[camera_model] += 1

        for cond_key, cond_val in conditions.items():
            cond_label = f"{cond_key}={cond_val}"
            condition_counts[cond_key][str(cond_val)] += 1
            class_per_condition[cond_label][obj_class] += 1

    # Gap analysis
    gaps: list[dict[str, Any]] = []
    sorted_sites = sorted(sites)
    for obj_class in CANONICAL_CLASSES:
        for site_id in sorted_sites:
            count = class_per_site[site_id].get(obj_class, 0)
            if count < min_samples:
                gaps.append({
                    "object_class": obj_class,
                    "site_id": site_id,
                    "count": count,
                    "min_required": min_samples,
                    "deficit": min_samples - count,
                })
        # Check for completely missing classes
        if class_total.get(obj_class, 0) == 0:
            gaps.append({
                "object_class": obj_class,
                "site_id": "ALL",
                "count": 0,
                "min_required": min_samples,
                "deficit": min_samples,
            })

    # Missing conditions: check if any site lacks lighting or weather tags
    condition_gaps: list[str] = []
    for site_id in sorted_sites:
        site_items = [i for i in items if i.get("site_id") == site_id]
        if site_items:
            has_conditions = any(i.get("conditions") for i in site_items)
            if not has_conditions:
                condition_gaps.append(f"site {site_id}: no condition metadata")

    return {
        "total_items": len(items),
        "total_sites": len(sites),
        "total_cameras": len(cameras),
        "sites": sorted_sites,
        "class_total": dict(class_total.most_common()),
        "class_per_site": {s: dict(c.most_common()) for s, c in sorted(class_per_site.items())},
        "condition_distribution": {k: dict(v.most_common()) for k, v in sorted(condition_counts.items())},
        "class_per_condition": {c: dict(v.most_common()) for c, v in sorted(class_per_condition.items())},
        "camera_model_coverage": dict(camera_model_counts.most_common()),
        "cameras_per_site": {s: len(cams) for s, cams in sorted(cameras_per_site.items())},
        "samples_per_camera": dict(samples_per_camera.most_common()),
        "samples_per_site": dict(samples_per_site.most_common()),
        "project_counts": dict(project_counts.most_common()),
        "gaps": gaps,
        "condition_gaps": condition_gaps,
        "min_samples_threshold": min_samples,
    }


# ---------------------------------------------------------------------------
# Markdown report generation
# ---------------------------------------------------------------------------


def generate_report(stats: dict[str, Any], manifest_path: str) -> str:
    """Generate a Markdown coverage report from computed statistics."""
    lines: list[str] = []

    lines.append("# Dataset Coverage Report")
    lines.append("")
    lines.append(f"Generated: {datetime.now(tz=timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    lines.append(f"Source manifest: `{manifest_path}`")
    lines.append("")

    # Overview
    lines.append("## Overview")
    lines.append("")
    lines.append(f"- **Total samples:** {stats['total_items']:,}")
    lines.append(f"- **Sites:** {stats['total_sites']}")
    lines.append(f"- **Cameras:** {stats['total_cameras']}")
    lines.append(f"- **Object classes with data:** {len(stats['class_total'])}")
    lines.append(f"- **Gap threshold:** {stats['min_samples_threshold']} samples per class per site")
    lines.append("")

    # Per-class totals
    lines.append("## Class Distribution (Total)")
    lines.append("")
    lines.append("| Class | Count | % |")
    lines.append("|-------|------:|--:|")
    total = max(stats["total_items"], 1)
    for cls in CANONICAL_CLASSES:
        count = stats["class_total"].get(cls, 0)
        pct = count / total * 100
        lines.append(f"| {cls} | {count:,} | {pct:.1f}% |")
    lines.append("")

    # Per-site class distribution
    lines.append("## Per-Site Class Distribution")
    lines.append("")
    site_cols = stats["sites"]
    header = "| Class | " + " | ".join(site_cols) + " |"
    separator = "|-------|" + "|".join(["------:" for _ in site_cols]) + "|"
    lines.append(header)
    lines.append(separator)
    for cls in CANONICAL_CLASSES:
        row = f"| {cls} |"
        for site_id in site_cols:
            count = stats["class_per_site"].get(site_id, {}).get(cls, 0)
            row += f" {count:,} |"
        lines.append(row)
    lines.append("")

    # Condition coverage
    if stats["condition_distribution"]:
        lines.append("## Condition Coverage")
        lines.append("")
        for cond_key, values in stats["condition_distribution"].items():
            lines.append(f"### {cond_key.title()}")
            lines.append("")
            lines.append("| Value | Count |")
            lines.append("|-------|------:|")
            for val, count in values.items():
                lines.append(f"| {val} | {count:,} |")
            lines.append("")

    # Camera model coverage
    if stats["camera_model_coverage"]:
        lines.append("## Camera Model Coverage")
        lines.append("")
        lines.append("| Model | Samples |")
        lines.append("|-------|--------:|")
        for model, count in stats["camera_model_coverage"].items():
            lines.append(f"| {model} | {count:,} |")
        lines.append("")

    # Cameras per site
    lines.append("## Camera Coverage by Site")
    lines.append("")
    lines.append("| Site | Cameras | Samples |")
    lines.append("|------|--------:|--------:|")
    for site_id in site_cols:
        cam_count = stats["cameras_per_site"].get(site_id, 0)
        sample_count = stats["samples_per_site"].get(site_id, 0)
        lines.append(f"| {site_id} | {cam_count} | {sample_count:,} |")
    lines.append("")

    # Gap analysis
    lines.append("## Gap Analysis")
    lines.append("")
    gaps = stats["gaps"]
    if gaps:
        lines.append(f"**{len(gaps)} gap(s) found** (below {stats['min_samples_threshold']} samples):")
        lines.append("")
        lines.append("| Class | Site | Count | Deficit |")
        lines.append("|-------|------|------:|--------:|")
        for gap in gaps:
            lines.append(
                f"| {gap['object_class']} | {gap['site_id']} | "
                f"{gap['count']:,} | {gap['deficit']:,} |"
            )
        lines.append("")
    else:
        lines.append("No coverage gaps detected. All classes meet the minimum sample threshold at every site.")
        lines.append("")

    if stats["condition_gaps"]:
        lines.append("### Missing Condition Metadata")
        lines.append("")
        for gap in stats["condition_gaps"]:
            lines.append(f"- {gap}")
        lines.append("")

    # Recommendations
    lines.append("## Recommendations")
    lines.append("")
    if gaps:
        deficit_classes = sorted({g["object_class"] for g in gaps})
        deficit_sites = sorted({g["site_id"] for g in gaps if g["site_id"] != "ALL"})
        lines.append(f"- **Priority annotation targets:** {', '.join(deficit_classes)}")
        if deficit_sites:
            lines.append(f"- **Sites needing more data:** {', '.join(deficit_sites)}")
        lines.append("- Schedule targeted annotation sessions for underrepresented class/site combinations")
    else:
        lines.append("- Dataset meets minimum coverage thresholds")
    lines.append("- Review condition balance before training (day/night, weather diversity)")
    lines.append("- Ensure balanced splits preserve site and condition proportionality")
    lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    args = parse_args()
    manifest = load_manifest(args.manifest)
    items = manifest.get("items", [])
    if not items:
        raise ValueError("manifest contains no items")

    print(f"Analyzing {len(items)} items from manifest...")
    stats = compute_stats(items, args.min_samples)

    # Generate Markdown report
    report = generate_report(stats, str(args.manifest))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(report, encoding="utf-8")
    print(f"Coverage report: {args.output}")

    # Optional JSON output
    if args.output_json:
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        args.output_json.write_text(json.dumps(stats, indent=2) + "\n", encoding="utf-8")
        print(f"Statistics JSON: {args.output_json}")

    # Print summary
    print(f"\nSummary: {stats['total_items']} items, {stats['total_sites']} sites, "
          f"{stats['total_cameras']} cameras, {len(stats['gaps'])} gaps")

    if stats["gaps"]:
        print(f"\nWARNING: {len(stats['gaps'])} coverage gap(s) below {args.min_samples} samples:")
        for gap in stats["gaps"][:10]:
            print(f"  - {gap['object_class']} at {gap['site_id']}: {gap['count']}/{gap['min_required']}")
        if len(stats["gaps"]) > 10:
            print(f"  ... and {len(stats['gaps']) - 10} more")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        raise SystemExit(str(exc)) from exc
