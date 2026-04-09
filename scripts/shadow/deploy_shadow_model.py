#!/usr/bin/env python3
"""Deploy a candidate model to a Triton instance in shadow mode.

Usage:
    python deploy_shadow_model.py --model-name yolov8l --version 2 \
        --engine-path candidate/model.plan --triton-url http://localhost:8000 \
        --model-repo /models
"""

from __future__ import annotations

import argparse
import json
import shutil
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-name", required=True, help="Triton model name.")
    parser.add_argument("--version", required=True, help="Numeric model version.")
    parser.add_argument(
        "--engine-path",
        type=Path,
        help="Path to candidate TensorRT engine (`model.plan`) for load mode.",
    )
    parser.add_argument(
        "--triton-url",
        default="http://localhost:8000",
        help="Base Triton HTTP URL.",
    )
    parser.add_argument(
        "--model-repo",
        type=Path,
        required=True,
        help="Active Triton model repository root.",
    )
    parser.add_argument(
        "--unload",
        action="store_true",
        help="Unload the specified version instead of copying and loading it.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    triton_url = args.triton_url.rstrip("/")
    version_dir = args.model_repo / args.model_name / args.version

    if args.unload:
        unload_shadow_version(
            model_name=args.model_name,
            version=args.version,
            version_dir=version_dir,
            triton_url=triton_url,
        )
        print(
            f"Unloaded shadow version {args.version} for model {args.model_name}.",
        )
        return

    if args.engine_path is None:
        raise SystemExit("--engine-path is required unless --unload is set")

    deploy_shadow_version(
        model_name=args.model_name,
        version=args.version,
        engine_path=args.engine_path,
        version_dir=version_dir,
        triton_url=triton_url,
    )
    print(
        f"Shadow version {args.version} for {args.model_name} is loaded and ready.",
    )


def deploy_shadow_version(
    *,
    model_name: str,
    version: str,
    engine_path: Path,
    version_dir: Path,
    triton_url: str,
) -> None:
    if not engine_path.is_file():
        raise SystemExit(f"engine file not found: {engine_path}")

    version_dir.mkdir(parents=True, exist_ok=True)
    target_path = version_dir / "model.plan"
    shutil.copy2(engine_path, target_path)

    _triton_request(
        f"{triton_url}/v2/repository/models/{model_name}/load",
        method="POST",
    )
    metadata = _triton_request(
        f"{triton_url}/v2/models/{model_name}/versions/{version}",
        method="GET",
    )
    if not isinstance(metadata, dict):
        raise SystemExit(
            f"Triton returned unexpected metadata payload for {model_name}:{version}",
        )


def unload_shadow_version(
    *,
    model_name: str,
    version: str,
    version_dir: Path,
    triton_url: str,
) -> None:
    if version_dir.exists():
        shutil.rmtree(version_dir)

    _triton_request(
        f"{triton_url}/v2/repository/models/{model_name}/unload",
        method="POST",
    )

    remaining_versions = sorted(
        path.name
        for path in version_dir.parent.iterdir()
        if path.is_dir() and path.name.isdigit()
    ) if version_dir.parent.exists() else []
    if remaining_versions:
        _triton_request(
            f"{triton_url}/v2/repository/models/{model_name}/load",
            method="POST",
        )

    try:
        _triton_request(
            f"{triton_url}/v2/models/{model_name}/versions/{version}",
            method="GET",
        )
    except RuntimeError:
        return
    raise SystemExit(
        f"version {version} for model {model_name} still appears to be loaded",
    )


def _triton_request(
    url: str,
    *,
    method: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any] | list[Any] | None:
    data = None
    headers: dict[str, str] = {}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
            body = response.read().decode("utf-8").strip()
    except urllib.error.HTTPError as exc:  # pragma: no cover - runtime path
        detail = exc.read().decode("utf-8", errors="replace").strip()
        raise RuntimeError(
            f"Triton {method} {url} failed with HTTP {exc.code}: {detail}",
        ) from exc
    except urllib.error.URLError as exc:  # pragma: no cover - runtime path
        raise RuntimeError(f"failed to reach Triton at {url}: {exc.reason}") from exc

    if not body:
        return None
    return json.loads(body)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        raise SystemExit(str(exc)) from exc
