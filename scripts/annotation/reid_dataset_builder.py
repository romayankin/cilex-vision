#!/usr/bin/env python3
"""Build the final Re-ID training dataset from mined triplets.

Usage:
    python reid_dataset_builder.py --manifest data/reid-training/raw/triplet-manifest.json \
        --validation data/reid-training/validation-report.json \
        --output-dir data/reid-training/final --version v1
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from hard_example_miner import build_minio_client
from validate_reid_pairs import ensure_crop_path, parse_iso8601


DEFAULT_MINIO_URL = "http://localhost:9000"
DEFAULT_MINIO_ACCESS_KEY = "minioadmin"
DEFAULT_MINIO_SECRET_KEY = "minioadmin123"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest",
        type=Path,
        required=True,
        help="Triplet manifest produced by collect_reid_training_data.py.",
    )
    parser.add_argument(
        "--validation",
        type=Path,
        help="Optional validation report from validate_reid_pairs.py.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="Directory receiving the final dataset.",
    )
    parser.add_argument(
        "--version",
        required=True,
        help="Dataset version label, e.g. v1.",
    )
    parser.add_argument(
        "--minio-url",
        default=os.environ.get("MINIO_URL", DEFAULT_MINIO_URL),
        help="MinIO base URL, used only if crop URIs need remote download.",
    )
    parser.add_argument(
        "--minio-access-key",
        default=os.environ.get("MINIO_ACCESS_KEY", DEFAULT_MINIO_ACCESS_KEY),
        help="MinIO access key.",
    )
    parser.add_argument(
        "--minio-secret-key",
        default=os.environ.get("MINIO_SECRET_KEY", DEFAULT_MINIO_SECRET_KEY),
        help="MinIO secret key.",
    )
    parser.add_argument(
        "--train-ratio",
        type=float,
        default=0.70,
        help="Chronological train split ratio.",
    )
    parser.add_argument(
        "--val-ratio",
        type=float,
        default=0.15,
        help="Chronological validation split ratio.",
    )
    parser.add_argument(
        "--dvc",
        action="store_true",
        help="Run `dvc add` on the produced dataset directory.",
    )
    return parser.parse_args()


def load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise RuntimeError(f"file not found: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError(f"expected a JSON object in {path}")
    return payload


def approved_triplet_ids(validation_payload: dict[str, Any]) -> set[str]:
    approved = validation_payload.get("approved_triplet_ids")
    if isinstance(approved, list):
        return {str(value) for value in approved}

    pair_results = validation_payload.get("pair_results")
    if not isinstance(pair_results, list):
        return set()

    by_triplet: dict[str, dict[str, bool]] = {}
    for result in pair_results:
        triplet_id = str(result.get("triplet_id", ""))
        pair_role = str(result.get("pair_role", ""))
        correct = bool(result.get("correct"))
        if not triplet_id or not pair_role:
            continue
        by_triplet.setdefault(triplet_id, {})[pair_role] = correct
    return {
        triplet_id
        for triplet_id, roles in by_triplet.items()
        if roles.get("positive") and roles.get("negative")
    }


def filter_triplets(
    triplets: list[dict[str, Any]],
    *,
    validation_payload: dict[str, Any] | None,
) -> tuple[list[dict[str, Any]], str]:
    if validation_payload is None:
        return triplets, "warning: no validation report supplied; using all triplets"

    approved = approved_triplet_ids(validation_payload)
    if not approved:
        raise RuntimeError(
            "validation report did not contain any approved triplets; refusing to build a training dataset"
        )
    filtered = [triplet for triplet in triplets if str(triplet.get("triplet_id")) in approved]
    if not filtered:
        raise RuntimeError("no triplets from the manifest were approved by the validation report")
    return filtered, ""


def parse_s3_uri(uri: str) -> tuple[str, str]:
    if not uri.startswith("s3://"):
        raise RuntimeError(f"unsupported URI {uri!r}; expected s3://bucket/key")
    remainder = uri[5:]
    if "/" not in remainder:
        raise RuntimeError(f"unsupported URI {uri!r}; expected s3://bucket/key")
    return remainder.split("/", 1)


def download_minio_object(minio_client: Any, uri: str) -> bytes:
    bucket, object_name = parse_s3_uri(uri)
    response = minio_client.get_object(bucket, object_name)
    try:
        return response.read()
    finally:
        response.close()
        response.release_conn()


def resolve_crop(
    triplet_id: str,
    role: str,
    endpoint: dict[str, Any],
    *,
    cache_dir: Path,
    minio_client: Any | None,
) -> Path:
    try:
        return ensure_crop_path(f"{triplet_id}-{role}", endpoint, cache_dir=cache_dir)
    except Exception as first_error:
        crop_uri = endpoint.get("crop_uri")
        if crop_uri and str(crop_uri).startswith("file://"):
            file_path = Path(str(crop_uri)[7:])
            if file_path.exists():
                return file_path

        frame_uri = endpoint.get("frame_uri")
        bbox = endpoint.get("representative_bbox_xyxy") or endpoint.get("representative_bbox_xywh")
        if minio_client is None or not frame_uri or not bbox:
            raise RuntimeError(
                f"unable to resolve crop for triplet {triplet_id} role {role}: {first_error}"
            ) from first_error

        from PIL import Image

        frame_bytes = download_minio_object(minio_client, str(frame_uri))
        cache_dir.mkdir(parents=True, exist_ok=True)
        generated_path = cache_dir / f"{triplet_id}-{role}.jpg"
        if generated_path.exists():
            return generated_path
        import io

        with Image.open(io.BytesIO(frame_bytes)) as image:
            if endpoint.get("representative_bbox_xyxy"):
                x1, y1, x2, y2 = [float(value) for value in bbox]
            else:
                x, y, w, h = [float(value) for value in bbox]
                x1, y1, x2, y2 = (x, y, x + w, y + h)
            width, height = image.size
            left = max(0, min(int(round(x1)), width))
            top = max(0, min(int(round(y1)), height))
            right = max(left + 1, min(int(round(x2)), width))
            bottom = max(top + 1, min(int(round(y2)), height))
            crop = image.crop((left, top, right, bottom))
            crop.save(generated_path, format="JPEG", quality=95)
        return generated_path


def assign_split(
    index: int,
    total: int,
    *,
    train_ratio: float,
    val_ratio: float,
) -> str:
    if total <= 0:
        raise RuntimeError("cannot assign a split to an empty triplet set")
    if total == 1:
        return "train"
    if total == 2:
        return "train" if index == 0 else "test"
    fraction = (index + 1) / total
    if fraction <= train_ratio:
        return "train"
    if fraction <= train_ratio + val_ratio:
        return "val"
    return "test"


def copy_triplet_assets(
    triplet: dict[str, Any],
    *,
    dataset_root: Path,
    split_name: str,
    cache_dir: Path,
    minio_client: Any | None,
) -> dict[str, str]:
    triplet_id = str(triplet["triplet_id"])
    copied_paths: dict[str, str] = {}
    for role in ("anchor", "positive", "negative"):
        endpoint = triplet.get(role)
        if not isinstance(endpoint, dict):
            raise RuntimeError(f"triplet {triplet_id} is missing role payload {role!r}")
        source_crop = resolve_crop(
            triplet_id,
            role,
            endpoint,
            cache_dir=cache_dir,
            minio_client=minio_client,
        )
        destination = dataset_root / split_name / role / f"{triplet_id}.jpg"
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_crop, destination)
        copied_paths[f"{role}_path"] = str(destination.resolve())
    return copied_paths


def build_manifest_item(
    triplet: dict[str, Any],
    *,
    split_name: str,
    copied_paths: dict[str, str],
) -> dict[str, Any]:
    anchor = triplet["anchor"]
    positive = triplet["positive"]
    negative = triplet["negative"]
    return {
        "item_id": triplet["triplet_id"],
        "triplet_id": triplet["triplet_id"],
        "split": split_name,
        "object_class": triplet["object_class"],
        "capture_ts": anchor["reference_time"],
        "sequence_id": f"{anchor['camera_id']}__{positive['camera_id']}",
        "source_uri": copied_paths["anchor_path"],
        "anchor": {
            "local_track_id": anchor["local_track_id"],
            "camera_id": anchor["camera_id"],
            "crop_path": copied_paths["anchor_path"],
        },
        "positive": {
            "local_track_id": positive["local_track_id"],
            "camera_id": positive["camera_id"],
            "crop_path": copied_paths["positive_path"],
        },
        "negative": {
            "local_track_id": negative["local_track_id"],
            "camera_id": negative["camera_id"],
            "crop_path": copied_paths["negative_path"],
        },
        "positive_link_confidence": triplet["positive_link_confidence"],
        "negative_rank": triplet["negative_rank"],
        "validation_pair_ids": triplet["validation_pair_ids"],
    }


def ensure_dvc_installed() -> None:
    if shutil.which("dvc") is None:
        raise RuntimeError("dvc is not installed or not on PATH; install with: pip install dvc")


def run_dvc_add(dataset_root: Path) -> str:
    ensure_dvc_installed()
    result = subprocess.run(
        ["dvc", "add", str(dataset_root)],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"dvc add failed: {result.stderr.strip() or result.stdout.strip()}")
    dvc_path = dataset_root.parent / f"{dataset_root.name}.dvc"
    if not dvc_path.exists():
        raise RuntimeError(f"dvc add succeeded but {dvc_path} was not created")
    return str(dvc_path)


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    args = parse_args()
    ratio_sum = args.train_ratio + args.val_ratio
    if args.train_ratio <= 0 or args.val_ratio < 0 or ratio_sum >= 1.0:
        raise RuntimeError("--train-ratio and --val-ratio must leave room for a test split")

    manifest_payload = load_json(args.manifest)
    triplets_raw = manifest_payload.get("triplets")
    if not isinstance(triplets_raw, list) or not triplets_raw:
        raise RuntimeError("triplet manifest must contain a non-empty triplets list")

    validation_payload = load_json(args.validation) if args.validation else None
    filtered_triplets, warning_message = filter_triplets(
        [triplet for triplet in triplets_raw if isinstance(triplet, dict)],
        validation_payload=validation_payload,
    )
    filtered_triplets.sort(key=lambda item: parse_iso8601(str(item["anchor"]["reference_time"])))

    dataset_root = args.output_dir.resolve() / args.version
    cache_dir = dataset_root / ".cache"
    manifest_items: list[dict[str, Any]] = []
    split_counts = {"train": 0, "val": 0, "test": 0}
    skipped_triplet_ids: list[str] = []
    minio_client = None
    if any(
        (
            isinstance(triplet.get(role), dict)
            and str(triplet[role].get("frame_uri") or "").startswith("s3://")
        )
        for triplet in filtered_triplets
        for role in ("anchor", "positive", "negative")
    ):
        minio_client = build_minio_client(args)

    for index, triplet in enumerate(filtered_triplets):
        split_name = assign_split(
            index,
            len(filtered_triplets),
            train_ratio=args.train_ratio,
            val_ratio=args.val_ratio,
        )
        try:
            copied_paths = copy_triplet_assets(
                triplet,
                dataset_root=dataset_root,
                split_name=split_name,
                cache_dir=cache_dir,
                minio_client=minio_client,
            )
        except Exception:
            skipped_triplet_ids.append(str(triplet.get("triplet_id", "unknown-triplet")))
            continue
        manifest_items.append(
            build_manifest_item(
                triplet,
                split_name=split_name,
                copied_paths=copied_paths,
            )
        )
        split_counts[split_name] += 1

    if not manifest_items:
        raise RuntimeError("no triplets were available after filtering and asset resolution")

    train_items = [item for item in manifest_items if item["split"] == "train"]
    val_items = [item for item in manifest_items if item["split"] == "val"]
    test_items = [item for item in manifest_items if item["split"] == "test"]

    manifest_payload_out = {
        "generated_at": datetime.now(UTC).isoformat(),
        "version": args.version,
        "source_manifest": str(args.manifest),
        "validation_report": str(args.validation) if args.validation else None,
        "warning": warning_message or None,
        "counts": {
            "total": len(manifest_items),
            **split_counts,
            "skipped_unresolved_assets": len(skipped_triplet_ids),
        },
        "items": manifest_items,
        "splits": {
            "train": [item["triplet_id"] for item in train_items],
            "val": [item["triplet_id"] for item in val_items],
            "test": [item["triplet_id"] for item in test_items],
        },
        "skipped_triplet_ids": skipped_triplet_ids,
    }

    write_json(dataset_root / "manifest.json", manifest_payload_out)
    write_json(dataset_root / "train.json", {"items": train_items})
    write_json(dataset_root / "val.json", {"items": val_items})
    write_json(dataset_root / "test.json", {"items": test_items})

    dvc_file = None
    if args.dvc:
        dvc_file = run_dvc_add(dataset_root)

    summary = {
        "dataset_root": str(dataset_root),
        "manifest": str(dataset_root / "manifest.json"),
        "counts": manifest_payload_out["counts"],
        "dvc_file": dvc_file,
    }
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        raise SystemExit(str(exc)) from exc
