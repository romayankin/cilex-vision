#!/usr/bin/env python3
"""Re-ID evaluation metric computation functions.

Pure helpers for evaluating cross-camera Re-ID associations against ground
truth identity groups. This module intentionally contains no database or
network I/O so it can be reused by CLI scripts and tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations
from typing import Any


@dataclass(frozen=True)
class IdentitySighting:
    local_track_id: str
    camera_id: str
    timestamp: str | None = None
    crop_uri: str | None = None
    object_class: str | None = None


@dataclass(frozen=True)
class IdentityGroup:
    global_id: str
    sightings: tuple[IdentitySighting, ...]


@dataclass(frozen=True)
class PredictedAssociation:
    local_track_id: str
    global_track_id: str
    camera_id: str
    confidence: float
    linked_at: str | None = None
    object_class: str | None = None


@dataclass(frozen=True)
class QueryResult:
    query_track_id: str
    true_match_ids: frozenset[str]
    ranked_results: tuple[tuple[str, float], ...]


@dataclass(frozen=True)
class CameraPairMetrics:
    camera_a: str
    camera_b: str
    true_pairs: int
    predicted_pairs: int
    correct: int
    precision: float
    recall: float


@dataclass(frozen=True)
class ReIDMetrics:
    rank1_accuracy: float
    rank5_accuracy: float
    mean_average_precision: float
    false_positive_rate: float
    false_negative_rate: float
    precision: float
    recall: float
    f1: float
    total_queries: int
    total_true_pairs: int
    total_predicted_pairs: int
    per_camera_pair: dict[str, CameraPairMetrics]


def compute_rank_accuracy(
    query_track_id: str,
    true_match_ids: set[str],
    ranked_results: list[tuple[str, float]],
    k: int,
) -> bool:
    """Return whether any true match appears within the first ``k`` results."""
    if not query_track_id:
        raise ValueError("query_track_id must be non-empty")
    if k <= 0:
        raise ValueError("k must be positive")
    if not true_match_ids:
        return False

    seen_track_ids: set[str] = set()
    top_ranked_ids: list[str] = []
    for track_id, _score in ranked_results:
        if track_id in seen_track_ids:
            continue
        seen_track_ids.add(track_id)
        top_ranked_ids.append(track_id)
        if len(top_ranked_ids) >= k:
            break
    return any(track_id in true_match_ids for track_id in top_ranked_ids)


def compute_mean_average_precision(queries: list[QueryResult]) -> float:
    """Compute the mean average precision across all query results."""
    if not queries:
        return 0.0

    average_precisions: list[float] = []
    for query in queries:
        if not query.true_match_ids:
            continue
        hits = 0
        precision_sum = 0.0
        seen_track_ids: set[str] = set()
        for rank, (track_id, _score) in enumerate(query.ranked_results, start=1):
            if track_id in seen_track_ids:
                continue
            seen_track_ids.add(track_id)
            if track_id in query.true_match_ids:
                hits += 1
                precision_sum += hits / rank
        average_precisions.append(precision_sum / len(query.true_match_ids))

    if not average_precisions:
        return 0.0
    return sum(average_precisions) / len(average_precisions)


def compute_reid_metrics(
    ground_truth: list[IdentityGroup],
    predictions: list[PredictedAssociation],
) -> ReIDMetrics:
    """Compute MTMC Re-ID metrics from ground truth identities and predictions."""
    _build_ground_truth_track_index(ground_truth)
    predicted_by_track = _deduplicate_predictions(predictions)
    predicted_by_global: dict[str, list[PredictedAssociation]] = {}
    for association in predicted_by_track.values():
        predicted_by_global.setdefault(association.global_track_id, []).append(association)

    queries = _build_query_results(ground_truth, predicted_by_track, predicted_by_global)
    rank1_hits = sum(
        compute_rank_accuracy(
            query.query_track_id,
            set(query.true_match_ids),
            list(query.ranked_results),
            1,
        )
        for query in queries
    )
    rank5_hits = sum(
        compute_rank_accuracy(
            query.query_track_id,
            set(query.true_match_ids),
            list(query.ranked_results),
            5,
        )
        for query in queries
    )
    rank1_accuracy = rank1_hits / len(queries) if queries else 0.0
    rank5_accuracy = rank5_hits / len(queries) if queries else 0.0
    mean_average_precision = compute_mean_average_precision(queries)

    true_pairs, true_pairs_by_camera = _build_true_pair_sets(ground_truth)
    predicted_pairs, predicted_pairs_by_camera = _build_predicted_pair_sets(predicted_by_global)

    correct_pairs = true_pairs & predicted_pairs
    false_positive_pairs = predicted_pairs - true_pairs
    false_negative_pairs = true_pairs - predicted_pairs

    total_true_pairs = len(true_pairs)
    total_predicted_pairs = len(predicted_pairs)
    correct_count = len(correct_pairs)

    precision = (
        correct_count / total_predicted_pairs if total_predicted_pairs > 0 else 0.0
    )
    recall = correct_count / total_true_pairs if total_true_pairs > 0 else 0.0
    f1 = (
        2 * precision * recall / (precision + recall)
        if precision > 0.0 and recall > 0.0
        else 0.0
    )
    false_positive_rate = (
        len(false_positive_pairs) / total_predicted_pairs if total_predicted_pairs > 0 else 0.0
    )
    false_negative_rate = (
        len(false_negative_pairs) / total_true_pairs if total_true_pairs > 0 else 0.0
    )

    camera_keys = sorted(set(true_pairs_by_camera) | set(predicted_pairs_by_camera))
    per_camera_pair: dict[str, CameraPairMetrics] = {}
    for camera_key in camera_keys:
        camera_a, camera_b = camera_key.split("|", maxsplit=1)
        true_camera_pairs = true_pairs_by_camera.get(camera_key, set())
        predicted_camera_pairs = predicted_pairs_by_camera.get(camera_key, set())
        correct_camera_pairs = true_camera_pairs & predicted_camera_pairs
        camera_precision = (
            len(correct_camera_pairs) / len(predicted_camera_pairs)
            if predicted_camera_pairs
            else 0.0
        )
        camera_recall = (
            len(correct_camera_pairs) / len(true_camera_pairs) if true_camera_pairs else 0.0
        )
        per_camera_pair[camera_key] = CameraPairMetrics(
            camera_a=camera_a,
            camera_b=camera_b,
            true_pairs=len(true_camera_pairs),
            predicted_pairs=len(predicted_camera_pairs),
            correct=len(correct_camera_pairs),
            precision=camera_precision,
            recall=camera_recall,
        )

    return ReIDMetrics(
        rank1_accuracy=rank1_accuracy,
        rank5_accuracy=rank5_accuracy,
        mean_average_precision=mean_average_precision,
        false_positive_rate=false_positive_rate,
        false_negative_rate=false_negative_rate,
        precision=precision,
        recall=recall,
        f1=f1,
        total_queries=len(queries),
        total_true_pairs=total_true_pairs,
        total_predicted_pairs=total_predicted_pairs,
        per_camera_pair=per_camera_pair,
    )


def identity_groups_from_payload(payload: dict[str, Any]) -> list[IdentityGroup]:
    """Parse an evaluation ground-truth payload into typed identity groups."""
    raw_groups = payload.get("identity_groups")
    if not isinstance(raw_groups, list):
        raise ValueError("ground truth payload must contain an identity_groups list")

    groups: list[IdentityGroup] = []
    for raw_group in raw_groups:
        if not isinstance(raw_group, dict):
            raise ValueError("each identity group must be an object")
        global_id = _require_non_empty_string(raw_group.get("global_id"), "global_id")
        raw_sightings = raw_group.get("sightings")
        if not isinstance(raw_sightings, list):
            raise ValueError(f"identity group {global_id!r} must contain a sightings list")
        sightings: list[IdentitySighting] = []
        for raw_sighting in raw_sightings:
            if not isinstance(raw_sighting, dict):
                raise ValueError(f"identity group {global_id!r} contains a non-object sighting")
            sightings.append(
                IdentitySighting(
                    local_track_id=_require_non_empty_string(
                        raw_sighting.get("local_track_id"),
                        "local_track_id",
                    ),
                    camera_id=_require_non_empty_string(
                        raw_sighting.get("camera_id"),
                        "camera_id",
                    ),
                    timestamp=_optional_string(raw_sighting.get("timestamp")),
                    crop_uri=_optional_string(raw_sighting.get("crop_uri")),
                    object_class=_optional_string(raw_sighting.get("object_class")),
                )
            )
        groups.append(IdentityGroup(global_id=global_id, sightings=tuple(sightings)))
    return groups


def metrics_to_json_dict(metrics: ReIDMetrics) -> dict[str, Any]:
    """Convert ``ReIDMetrics`` into a JSON-safe dictionary."""
    return {
        "rank1_accuracy": metrics.rank1_accuracy,
        "rank5_accuracy": metrics.rank5_accuracy,
        "mean_average_precision": metrics.mean_average_precision,
        "false_positive_rate": metrics.false_positive_rate,
        "false_negative_rate": metrics.false_negative_rate,
        "precision": metrics.precision,
        "recall": metrics.recall,
        "f1": metrics.f1,
        "total_queries": metrics.total_queries,
        "total_true_pairs": metrics.total_true_pairs,
        "total_predicted_pairs": metrics.total_predicted_pairs,
        "per_camera_pair": {
            key: {
                "camera_a": value.camera_a,
                "camera_b": value.camera_b,
                "true_pairs": value.true_pairs,
                "predicted_pairs": value.predicted_pairs,
                "correct": value.correct,
                "precision": value.precision,
                "recall": value.recall,
            }
            for key, value in metrics.per_camera_pair.items()
        },
    }


def _build_ground_truth_track_index(
    ground_truth: list[IdentityGroup],
) -> dict[str, IdentitySighting]:
    gt_track_to_sighting: dict[str, IdentitySighting] = {}
    for identity_group in ground_truth:
        for sighting in identity_group.sightings:
            if sighting.local_track_id in gt_track_to_sighting:
                raise ValueError(
                    f"duplicate local_track_id in ground truth: {sighting.local_track_id}"
                )
            gt_track_to_sighting[sighting.local_track_id] = sighting
    return gt_track_to_sighting


def _deduplicate_predictions(
    predictions: list[PredictedAssociation],
) -> dict[str, PredictedAssociation]:
    deduplicated: dict[str, PredictedAssociation] = {}
    for association in predictions:
        existing = deduplicated.get(association.local_track_id)
        if existing is None or association.confidence > existing.confidence:
            deduplicated[association.local_track_id] = association
    return deduplicated


def _build_query_results(
    ground_truth: list[IdentityGroup],
    predicted_by_track: dict[str, PredictedAssociation],
    predicted_by_global: dict[str, list[PredictedAssociation]],
) -> list[QueryResult]:
    queries: list[QueryResult] = []
    for identity_group in ground_truth:
        for sighting in identity_group.sightings:
            true_match_ids = frozenset(
                other.local_track_id
                for other in identity_group.sightings
                if other.local_track_id != sighting.local_track_id
                and other.camera_id != sighting.camera_id
            )
            if not true_match_ids:
                continue

            query_prediction = predicted_by_track.get(sighting.local_track_id)
            ranked_results: list[tuple[str, float]] = []
            if query_prediction is not None:
                candidate_predictions = predicted_by_global.get(
                    query_prediction.global_track_id,
                    [],
                )
                for candidate in candidate_predictions:
                    if candidate.local_track_id == sighting.local_track_id:
                        continue
                    if candidate.camera_id == sighting.camera_id:
                        continue
                    ranked_results.append(
                        (
                            candidate.local_track_id,
                            min(query_prediction.confidence, candidate.confidence),
                        )
                    )
                ranked_results.sort(key=lambda item: (-item[1], item[0]))

            queries.append(
                QueryResult(
                    query_track_id=sighting.local_track_id,
                    true_match_ids=true_match_ids,
                    ranked_results=tuple(ranked_results),
                )
            )
    return queries


def _build_true_pair_sets(
    ground_truth: list[IdentityGroup],
) -> tuple[set[tuple[str, str]], dict[str, set[tuple[str, str]]]]:
    pair_set: set[tuple[str, str]] = set()
    pair_by_camera: dict[str, set[tuple[str, str]]] = {}
    for identity_group in ground_truth:
        for left, right in combinations(identity_group.sightings, 2):
            if left.camera_id == right.camera_id:
                continue
            track_pair = _normalize_track_pair(left.local_track_id, right.local_track_id)
            camera_key = _normalize_camera_pair(left.camera_id, right.camera_id)
            pair_set.add(track_pair)
            pair_by_camera.setdefault(camera_key, set()).add(track_pair)
    return pair_set, pair_by_camera


def _build_predicted_pair_sets(
    predicted_by_global: dict[str, list[PredictedAssociation]],
) -> tuple[set[tuple[str, str]], dict[str, set[tuple[str, str]]]]:
    pair_set: set[tuple[str, str]] = set()
    pair_by_camera: dict[str, set[tuple[str, str]]] = {}
    for association_group in predicted_by_global.values():
        for left, right in combinations(association_group, 2):
            if left.camera_id == right.camera_id:
                continue
            track_pair = _normalize_track_pair(left.local_track_id, right.local_track_id)
            camera_key = _normalize_camera_pair(left.camera_id, right.camera_id)
            pair_set.add(track_pair)
            pair_by_camera.setdefault(camera_key, set()).add(track_pair)
    return pair_set, pair_by_camera


def _normalize_track_pair(track_a: str, track_b: str) -> tuple[str, str]:
    return tuple(sorted((track_a, track_b)))


def _normalize_camera_pair(camera_a: str, camera_b: str) -> str:
    ordered = sorted((camera_a, camera_b))
    return f"{ordered[0]}|{ordered[1]}"


def _require_non_empty_string(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value.strip()


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("optional string field must be a string when present")
    stripped = value.strip()
    return stripped if stripped else None


__all__ = [
    "CameraPairMetrics",
    "IdentityGroup",
    "IdentitySighting",
    "PredictedAssociation",
    "QueryResult",
    "ReIDMetrics",
    "compute_mean_average_precision",
    "compute_rank_accuracy",
    "compute_reid_metrics",
    "identity_groups_from_payload",
    "metrics_to_json_dict",
]
