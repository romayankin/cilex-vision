#!/usr/bin/env python3
"""Compute inter-annotator agreement for pilot annotation batches.

Expected input: one normalized JSON bundle per annotator.

Example:
{
  "annotator_id": "reviewer-a",
  "items": [
    {
      "item_id": "cam-01:000001",
      "frame_index": 1,
      "timestamp": "2026-04-06T10:15:00Z",
      "annotations": [
        {
          "instance_key": "track-17",
          "bbox_xywh": [100, 120, 54, 130],
          "object_class": "person",
          "attributes": {
            "person_upper_color": "green",
            "person_lower_color": "black"
          }
        }
      ]
    }
  ]
}

Outputs:
- JSON scorecard
- Markdown summary

Flags are raised when:
- mean box IoU < 0.65
- class Cohen's kappa < 0.60
- color Fleiss' kappa < 0.60
"""

from __future__ import annotations

import argparse
import itertools
import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from statistics import mean
from typing import Any


OBJECT_CLASSES: tuple[str, ...] = (
    "person",
    "car",
    "truck",
    "bus",
    "bicycle",
    "motorcycle",
    "animal",
)
COLOR_VALUES: tuple[str, ...] = (
    "red",
    "blue",
    "white",
    "black",
    "silver",
    "green",
    "yellow",
    "brown",
    "orange",
    "unknown",
)
ATTRIBUTE_SCOPES: dict[str, tuple[str, ...]] = {
    "vehicle_color": ("car", "truck", "bus", "motorcycle"),
    "person_upper_color": ("person",),
    "person_lower_color": ("person",),
}
MISSING_CATEGORY = "__missing__"


@dataclass(frozen=True)
class AnnotationRecord:
    annotator_id: str
    item_id: str
    frame_index: int | None
    timestamp: str | None
    instance_key: str | None
    bbox_xywh: tuple[float, float, float, float]
    object_class: str
    attributes: dict[str, str]


@dataclass(frozen=True)
class Bundle:
    annotator_id: str
    path: Path
    items: dict[str, list[AnnotationRecord]]


@dataclass(frozen=True)
class MatchedPair:
    anchor: AnnotationRecord
    other: AnnotationRecord
    iou: float


@dataclass
class Group:
    item_id: str
    group_id: str
    ratings: dict[str, AnnotationRecord | None] = field(default_factory=dict)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "bundles",
        nargs="+",
        type=Path,
        help="One normalized annotation JSON file per annotator.",
    )
    parser.add_argument(
        "--pairing-iou-threshold",
        type=float,
        default=0.10,
        help="Minimum IoU used when pairing annotations without shared instance keys.",
    )
    parser.add_argument(
        "--box-threshold",
        type=float,
        default=0.65,
        help="Minimum acceptable mean box IoU.",
    )
    parser.add_argument(
        "--kappa-threshold",
        type=float,
        default=0.60,
        help="Minimum acceptable class/color kappa.",
    )
    parser.add_argument(
        "--output-json",
        type=Path,
        default=Path("data/reports/annotation/iaa-scorecard.json"),
        help="Path for the JSON scorecard.",
    )
    parser.add_argument(
        "--output-markdown",
        type=Path,
        default=Path("data/reports/annotation/iaa-scorecard.md"),
        help="Path for the Markdown scorecard.",
    )
    return parser.parse_args()


def xywh_to_xyxy(box: tuple[float, float, float, float]) -> tuple[float, float, float, float]:
    x, y, width, height = box
    return (x, y, x + width, y + height)


def compute_iou(
    first: tuple[float, float, float, float],
    second: tuple[float, float, float, float],
) -> float:
    ax1, ay1, ax2, ay2 = xywh_to_xyxy(first)
    bx1, by1, bx2, by2 = xywh_to_xyxy(second)

    inter_x1 = max(ax1, bx1)
    inter_y1 = max(ay1, by1)
    inter_x2 = min(ax2, bx2)
    inter_y2 = min(ay2, by2)
    inter_width = max(0.0, inter_x2 - inter_x1)
    inter_height = max(0.0, inter_y2 - inter_y1)
    inter_area = inter_width * inter_height

    first_area = max(0.0, ax2 - ax1) * max(0.0, ay2 - ay1)
    second_area = max(0.0, bx2 - bx1) * max(0.0, by2 - by1)
    union_area = first_area + second_area - inter_area
    if union_area <= 0.0:
        return 0.0
    return inter_area / union_area


def validate_object_class(value: str) -> str:
    if value not in OBJECT_CLASSES:
        raise ValueError(f"invalid object_class {value!r}; expected one of {OBJECT_CLASSES}")
    return value


def validate_attributes(object_class: str, attributes: dict[str, Any]) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for name, value in attributes.items():
        if name not in ATTRIBUTE_SCOPES:
            raise ValueError(f"invalid attribute name {name!r}")
        if object_class not in ATTRIBUTE_SCOPES[name]:
            raise ValueError(f"attribute {name!r} is not valid for class {object_class!r}")
        if value not in COLOR_VALUES:
            raise ValueError(f"invalid color value {value!r}; expected one of {COLOR_VALUES}")
        normalized[name] = str(value)
    return normalized


def load_bundle(path: Path) -> Bundle:
    if not path.exists():
        raise FileNotFoundError(f"annotation bundle not found: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    annotator_id = payload.get("annotator_id")
    if not annotator_id:
        raise ValueError(f"{path}: annotator_id is required")
    items_raw = payload.get("items")
    if not isinstance(items_raw, list) or not items_raw:
        raise ValueError(f"{path}: items must be a non-empty list")

    items: dict[str, list[AnnotationRecord]] = defaultdict(list)
    for item in items_raw:
        item_id = item.get("item_id")
        if not item_id:
            raise ValueError(f"{path}: every item requires item_id")
        frame_index = item.get("frame_index")
        timestamp = item.get("timestamp")
        annotations = item.get("annotations")
        if not isinstance(annotations, list):
            raise ValueError(f"{path}: item {item_id!r} requires an annotations list")

        for index, annotation in enumerate(annotations):
            bbox = annotation.get("bbox_xywh")
            if not isinstance(bbox, list) or len(bbox) != 4:
                raise ValueError(f"{path}: annotation {index} in {item_id!r} requires bbox_xywh[4]")
            box = tuple(float(component) for component in bbox)
            if box[2] <= 0.0 or box[3] <= 0.0:
                raise ValueError(f"{path}: annotation {index} in {item_id!r} has non-positive width/height")

            object_class = validate_object_class(str(annotation.get("object_class", "")))
            attributes = validate_attributes(object_class, annotation.get("attributes", {}))
            instance_key = annotation.get("instance_key")
            record = AnnotationRecord(
                annotator_id=str(annotator_id),
                item_id=str(item_id),
                frame_index=int(frame_index) if frame_index is not None else None,
                timestamp=str(timestamp) if timestamp is not None else None,
                instance_key=str(instance_key) if instance_key is not None else None,
                bbox_xywh=box,
                object_class=object_class,
                attributes=attributes,
            )
            items[str(item_id)].append(record)

    return Bundle(annotator_id=str(annotator_id), path=path, items=dict(items))


def match_annotations(
    anchors: list[AnnotationRecord],
    others: list[AnnotationRecord],
    pairing_iou_threshold: float,
) -> tuple[list[MatchedPair], list[AnnotationRecord], list[AnnotationRecord]]:
    matched: list[MatchedPair] = []
    unmatched_anchor_indices = set(range(len(anchors)))
    unmatched_other_indices = set(range(len(others)))

    other_by_key: dict[str, list[int]] = defaultdict(list)
    for index, annotation in enumerate(others):
        if annotation.instance_key:
            other_by_key[annotation.instance_key].append(index)

    for anchor_index, anchor in enumerate(anchors):
        if not anchor.instance_key:
            continue
        candidate_indices = [
            index
            for index in other_by_key.get(anchor.instance_key, [])
            if index in unmatched_other_indices
        ]
        if len(candidate_indices) != 1:
            continue
        other_index = candidate_indices[0]
        matched.append(
            MatchedPair(
                anchor=anchor,
                other=others[other_index],
                iou=compute_iou(anchor.bbox_xywh, others[other_index].bbox_xywh),
            )
        )
        unmatched_anchor_indices.discard(anchor_index)
        unmatched_other_indices.discard(other_index)

    remaining_candidates: list[tuple[float, int, int]] = []
    for anchor_index in unmatched_anchor_indices:
        for other_index in unmatched_other_indices:
            iou = compute_iou(anchors[anchor_index].bbox_xywh, others[other_index].bbox_xywh)
            if iou >= pairing_iou_threshold:
                remaining_candidates.append((iou, anchor_index, other_index))
    remaining_candidates.sort(reverse=True)

    taken_anchor: set[int] = set()
    taken_other: set[int] = set()
    for iou, anchor_index, other_index in remaining_candidates:
        if anchor_index in taken_anchor or other_index in taken_other:
            continue
        matched.append(
            MatchedPair(
                anchor=anchors[anchor_index],
                other=others[other_index],
                iou=iou,
            )
        )
        taken_anchor.add(anchor_index)
        taken_other.add(other_index)

    unmatched_anchor = [
        anchors[index]
        for index in sorted(unmatched_anchor_indices - taken_anchor)
    ]
    unmatched_other = [
        others[index]
        for index in sorted(unmatched_other_indices - taken_other)
    ]
    return matched, unmatched_anchor, unmatched_other


def cohens_kappa(labels_a: list[str], labels_b: list[str], categories: tuple[str, ...]) -> float | None:
    if len(labels_a) != len(labels_b):
        raise ValueError("kappa inputs must have the same length")
    if not labels_a:
        return None

    observed = sum(1 for first, second in zip(labels_a, labels_b) if first == second) / len(labels_a)
    counts_a = Counter(labels_a)
    counts_b = Counter(labels_b)
    total = len(labels_a)
    expected = 0.0
    for category in categories:
        expected += (counts_a.get(category, 0) / total) * (counts_b.get(category, 0) / total)
    if expected >= 1.0:
        return 1.0 if observed >= 1.0 else 0.0
    return (observed - expected) / (1.0 - expected)


def majority_class(records: list[AnnotationRecord | None]) -> str | None:
    classes = [record.object_class for record in records if record is not None]
    if not classes:
        return None
    counts = Counter(classes)
    top_count = max(counts.values())
    return sorted(name for name, count in counts.items() if count == top_count)[0]


def build_groups(bundles: list[Bundle], pairing_iou_threshold: float) -> list[Group]:
    annotator_ids = [bundle.annotator_id for bundle in bundles]
    if len(set(annotator_ids)) != len(annotator_ids):
        raise ValueError("annotator_id values must be unique across input bundles")

    reference = bundles[0]
    groups_by_item: dict[str, list[Group]] = {}
    for item_id, annotations in reference.items.items():
        item_groups: list[Group] = []
        for index, annotation in enumerate(annotations):
            key = annotation.instance_key or f"{reference.annotator_id}-{index:04d}"
            item_groups.append(
                Group(
                    item_id=item_id,
                    group_id=key,
                    ratings={bundle.annotator_id: None for bundle in bundles},
                )
            )
            item_groups[-1].ratings[reference.annotator_id] = annotation
        groups_by_item[item_id] = item_groups

    known_item_ids = set(reference.items)
    for bundle in bundles[1:]:
        known_item_ids.update(bundle.items)

    for bundle in bundles[1:]:
        for item_id in sorted(known_item_ids):
            groups = groups_by_item.setdefault(item_id, [])
            candidate_groups = [group for group in groups if group.ratings.get(bundle.annotator_id) is None]
            annotations = bundle.items.get(item_id, [])

            synthetic_annotations: list[AnnotationRecord] = []
            group_lookup: dict[int, Group] = {}
            for group in candidate_groups:
                representative = next(
                    (group.ratings[annotator_id] for annotator_id in annotator_ids if group.ratings.get(annotator_id) is not None),
                    None,
                )
                if representative is None:
                    continue
                synthetic_annotations.append(representative)
                group_lookup[id(representative)] = group

            matched, _unmatched_group_annotations, unmatched_annotations = match_annotations(
                synthetic_annotations,
                annotations,
                pairing_iou_threshold,
            )
            for pair in matched:
                group_lookup[id(pair.anchor)].ratings[bundle.annotator_id] = pair.other

            next_index = len(groups)
            for annotation in unmatched_annotations:
                group = Group(
                    item_id=item_id,
                    group_id=annotation.instance_key or f"{bundle.annotator_id}-{next_index:04d}",
                    ratings={candidate.annotator_id: None for candidate in bundles},
                )
                group.ratings[bundle.annotator_id] = annotation
                groups.append(group)
                next_index += 1

    flattened: list[Group] = []
    for item_id in sorted(groups_by_item):
        flattened.extend(groups_by_item[item_id])
    return flattened


def compute_pairwise_metrics(
    first: Bundle,
    second: Bundle,
    pairing_iou_threshold: float,
) -> dict[str, Any]:
    item_ids = sorted(set(first.items) | set(second.items))
    ious: list[float] = []
    labels_a: list[str] = []
    labels_b: list[str] = []
    matched_count = 0
    unmatched_count = 0

    for item_id in item_ids:
        matched, unmatched_first, unmatched_second = match_annotations(
            first.items.get(item_id, []),
            second.items.get(item_id, []),
            pairing_iou_threshold,
        )
        for pair in matched:
            ious.append(pair.iou)
            labels_a.append(pair.anchor.object_class)
            labels_b.append(pair.other.object_class)
            matched_count += 1
        for annotation in unmatched_first:
            ious.append(0.0)
            labels_a.append(annotation.object_class)
            labels_b.append(MISSING_CATEGORY)
            unmatched_count += 1
        for annotation in unmatched_second:
            ious.append(0.0)
            labels_a.append(MISSING_CATEGORY)
            labels_b.append(annotation.object_class)
            unmatched_count += 1

    box_iou_mean = mean(ious) if ious else None
    class_kappa = cohens_kappa(labels_a, labels_b, OBJECT_CLASSES + (MISSING_CATEGORY,))
    pair_name = f"{first.annotator_id}__{second.annotator_id}"
    return {
        "pair": pair_name,
        "annotators": [first.annotator_id, second.annotator_id],
        "matched_count": matched_count,
        "unmatched_count": unmatched_count,
        "comparisons": len(labels_a),
        "box_iou_mean": box_iou_mean,
        "class_kappa": class_kappa,
    }


def fleiss_kappa(count_matrix: list[list[int]]) -> float | None:
    if not count_matrix:
        return None
    n_raters = sum(count_matrix[0])
    if n_raters <= 1:
        return None

    num_items = len(count_matrix)
    num_categories = len(count_matrix[0])
    for row in count_matrix:
        if sum(row) != n_raters:
            raise ValueError("each Fleiss row must have the same number of ratings")

    p_bar = 0.0
    for row in count_matrix:
        p_bar += (sum(value * value for value in row) - n_raters) / (n_raters * (n_raters - 1))
    p_bar /= num_items

    p_e = 0.0
    for category_index in range(num_categories):
        column_total = sum(row[category_index] for row in count_matrix)
        p_j = column_total / (num_items * n_raters)
        p_e += p_j * p_j

    if p_e >= 1.0:
        return 1.0 if p_bar >= 1.0 else 0.0
    return (p_bar - p_e) / (1.0 - p_e)


def compute_color_agreement(groups: list[Group], annotator_ids: list[str]) -> dict[str, Any]:
    categories = COLOR_VALUES + (MISSING_CATEGORY,)
    category_index = {name: index for index, name in enumerate(categories)}
    results: dict[str, Any] = {}

    for attribute_name in ATTRIBUTE_SCOPES:
        count_matrix: list[list[int]] = []
        rated_items = 0
        for group in groups:
            dominant_class = majority_class([group.ratings[annotator_id] for annotator_id in annotator_ids])
            if dominant_class is None or dominant_class not in ATTRIBUTE_SCOPES[attribute_name]:
                continue

            row = [0] * len(categories)
            for annotator_id in annotator_ids:
                record = group.ratings.get(annotator_id)
                if record is None:
                    row[category_index[MISSING_CATEGORY]] += 1
                    continue
                row[category_index[record.attributes.get(attribute_name, MISSING_CATEGORY)]] += 1
            count_matrix.append(row)
            rated_items += 1

        kappa = fleiss_kappa(count_matrix)
        results[attribute_name] = {
            "item_count": rated_items,
            "kappa": kappa,
        }

    valid_kappas = [value["kappa"] for value in results.values() if value["kappa"] is not None]
    results["summary"] = {
        "mean_kappa": mean(valid_kappas) if valid_kappas else None,
        "rated_attributes": len(valid_kappas),
    }
    return results


def build_flags(
    pairwise: list[dict[str, Any]],
    color_agreement: dict[str, Any],
    box_threshold: float,
    kappa_threshold: float,
) -> list[str]:
    flags: list[str] = []
    for pair in pairwise:
        if pair["box_iou_mean"] is not None and pair["box_iou_mean"] < box_threshold:
            flags.append(
                f"{pair['pair']}: mean box IoU {pair['box_iou_mean']:.4f} below {box_threshold:.2f}"
            )
        if pair["class_kappa"] is not None and pair["class_kappa"] < kappa_threshold:
            flags.append(
                f"{pair['pair']}: class kappa {pair['class_kappa']:.4f} below {kappa_threshold:.2f}"
            )

    for attribute_name, result in color_agreement.items():
        if attribute_name == "summary":
            continue
        kappa = result["kappa"]
        if kappa is not None and kappa < kappa_threshold:
            flags.append(
                f"{attribute_name}: Fleiss' kappa {kappa:.4f} below {kappa_threshold:.2f}"
            )
    return flags


def write_markdown(
    output_path: Path,
    summary: dict[str, Any],
    pairwise: list[dict[str, Any]],
    color_agreement: dict[str, Any],
    flags: list[str],
) -> None:
    lines = [
        "# Annotation IAA Scorecard",
        "",
        f"Annotators: {', '.join(summary['annotators'])}",
        "",
        "| Metric | Value | Threshold |",
        "|--------|-------|-----------|",
        f"| Mean box IoU | {format_metric(summary['mean_box_iou'])} | >= {summary['thresholds']['box_iou']:.2f} |",
        f"| Mean class Cohen's kappa | {format_metric(summary['mean_class_kappa'])} | >= {summary['thresholds']['kappa']:.2f} |",
        f"| Mean color Fleiss' kappa | {format_metric(summary['mean_color_kappa'])} | >= {summary['thresholds']['kappa']:.2f} |",
        "",
        "## Pairwise Agreement",
        "",
        "| Pair | Comparisons | Matched | Unmatched | Mean box IoU | Class kappa |",
        "|------|-------------|---------|-----------|--------------|-------------|",
    ]
    for pair in pairwise:
        lines.append(
            f"| {pair['pair']} | {pair['comparisons']} | {pair['matched_count']} | {pair['unmatched_count']} | "
            f"{format_metric(pair['box_iou_mean'])} | {format_metric(pair['class_kappa'])} |"
        )

    lines.extend(
        [
            "",
            "## Color Agreement",
            "",
            "| Attribute | Rated items | Fleiss' kappa |",
            "|-----------|-------------|---------------|",
        ]
    )
    for attribute_name in ("vehicle_color", "person_upper_color", "person_lower_color"):
        result = color_agreement[attribute_name]
        lines.append(
            f"| {attribute_name} | {result['item_count']} | {format_metric(result['kappa'])} |"
        )

    lines.extend(["", "## Flags", ""])
    if flags:
        lines.extend(f"- {flag}" for flag in flags)
    else:
        lines.append("- none")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def format_metric(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.4f}"


def main() -> None:
    args = parse_args()
    bundles = [load_bundle(path) for path in args.bundles]
    if len(bundles) < 2:
        raise SystemExit("at least two annotator bundles are required")

    pairwise = [
        compute_pairwise_metrics(first, second, args.pairing_iou_threshold)
        for first, second in itertools.combinations(bundles, 2)
    ]
    groups = build_groups(bundles, args.pairing_iou_threshold)
    color_agreement = compute_color_agreement(groups, [bundle.annotator_id for bundle in bundles])

    box_iou_values = [
        pair["box_iou_mean"]
        for pair in pairwise
        if pair["box_iou_mean"] is not None
    ]
    mean_box_iou = mean(box_iou_values) if box_iou_values else None
    class_kappas = [pair["class_kappa"] for pair in pairwise if pair["class_kappa"] is not None]
    mean_class_kappa = mean(class_kappas) if class_kappas else None
    mean_color_kappa = color_agreement["summary"]["mean_kappa"]
    flags = build_flags(pairwise, color_agreement, args.box_threshold, args.kappa_threshold)

    summary = {
        "annotators": [bundle.annotator_id for bundle in bundles],
        "bundle_paths": [str(bundle.path) for bundle in bundles],
        "group_count": len(groups),
        "pair_count": len(pairwise),
        "mean_box_iou": mean_box_iou,
        "mean_class_kappa": mean_class_kappa,
        "mean_color_kappa": mean_color_kappa,
        "thresholds": {
            "box_iou": args.box_threshold,
            "kappa": args.kappa_threshold,
            "pairing_iou": args.pairing_iou_threshold,
        },
        "status": "fail" if flags else "pass",
    }
    scorecard = {
        "summary": summary,
        "pairwise": pairwise,
        "color_agreement": color_agreement,
        "flags": flags,
    }

    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(json.dumps(scorecard, indent=2) + "\n", encoding="utf-8")
    write_markdown(args.output_markdown, summary, pairwise, color_agreement, flags)
    print(json.dumps(scorecard, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        raise SystemExit(str(exc)) from exc
