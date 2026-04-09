#!/usr/bin/env python3
"""Version a training dataset directory with DVC.

Runs `dvc add` on the dataset directory to create a .dvc tracking file,
then optionally pushes to a configured DVC remote.

Prerequisites:
    - DVC installed (`pip install dvc` or `pip install dvc[s3]`)
    - Git repository initialized
    - Optionally: DVC remote configured (`dvc remote add ...`)

Usage:
    python version_data.py --data-dir data/training/raw --version v1
    python version_data.py --data-dir data/training/raw --version v2 --remote minio
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def run_cmd(
    cmd: list[str],
    *,
    check: bool = True,
    capture: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run a shell command, returning the CompletedProcess result."""
    return subprocess.run(
        cmd,
        check=check,
        capture_output=capture,
        text=True,
    )


def ensure_dvc() -> None:
    """Verify DVC is installed and accessible."""
    if shutil.which("dvc") is None:
        raise SystemExit("dvc is not installed or not on PATH; install with: pip install dvc")


def dvc_add(data_dir: Path) -> Path:
    """Run `dvc add` on the data directory and return the .dvc file path."""
    result = run_cmd(["dvc", "add", str(data_dir)])
    if result.returncode != 0:
        raise RuntimeError(f"dvc add failed: {result.stderr}")

    dvc_file = data_dir.parent / f"{data_dir.name}.dvc"
    if not dvc_file.exists():
        raise RuntimeError(f"expected DVC file not found: {dvc_file}")

    return dvc_file


def dvc_push(remote: str | None = None) -> None:
    """Push tracked data to the DVC remote."""
    cmd = ["dvc", "push"]
    if remote:
        cmd.extend(["--remote", remote])

    result = run_cmd(cmd, check=False)
    if result.returncode != 0:
        print(f"warning: dvc push failed (remote may not be configured): {result.stderr}",
              file=sys.stderr)


def create_version_symlink(data_dir: Path, version: str) -> Path:
    """Create a versioned copy/symlink for the dataset.

    Copies the data directory to a versioned path (e.g., data/training/v1/)
    so that DVC tracks each version independently.
    """
    versioned_dir = data_dir.parent / version
    if versioned_dir.exists():
        print(f"versioned directory already exists: {versioned_dir}", file=sys.stderr)
        return versioned_dir

    # Use symlink to avoid duplicating data on disk
    versioned_dir.symlink_to(data_dir.name)
    return versioned_dir


def write_version_metadata(
    dvc_file: Path,
    version: str,
    data_dir: Path,
) -> dict[str, Any]:
    """Write version metadata alongside the DVC file."""
    metadata = {
        "version": version,
        "data_dir": str(data_dir),
        "dvc_file": str(dvc_file),
        "versioned_at": datetime.now(timezone.utc).isoformat(),
    }

    meta_path = dvc_file.parent / f"{version}_metadata.json"
    meta_path.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")

    return metadata


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-dir",
        type=Path,
        required=True,
        help="Path to the dataset directory to version.",
    )
    parser.add_argument(
        "--version",
        required=True,
        help="Version tag (e.g., v1, v2).",
    )
    parser.add_argument(
        "--remote",
        default=None,
        help="DVC remote name to push to (optional).",
    )
    parser.add_argument(
        "--no-push",
        action="store_true",
        help="Skip pushing to DVC remote.",
    )
    parser.add_argument(
        "--no-symlink",
        action="store_true",
        help="Skip creating versioned symlink.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    ensure_dvc()

    if not args.data_dir.exists():
        raise SystemExit(f"data directory does not exist: {args.data_dir}")

    # Create versioned symlink if requested
    target_dir = args.data_dir
    if not args.no_symlink:
        versioned_dir = create_version_symlink(args.data_dir, args.version)
        target_dir = versioned_dir
        print(f"created versioned path: {versioned_dir}")

    # Run DVC add
    dvc_file = dvc_add(target_dir)
    print(f"DVC tracking file: {dvc_file}")

    # Write metadata
    metadata = write_version_metadata(dvc_file, args.version, target_dir)

    # Push to remote
    if not args.no_push:
        dvc_push(args.remote)
        print("pushed to DVC remote")
    else:
        print("skipped DVC push (--no-push)")

    # Remind user to commit
    gitignore = target_dir.parent / ".gitignore"
    print("\nTo complete versioning, commit the DVC files:")
    print(f"  git add {dvc_file} {gitignore}")
    print(f"  git commit -m \"data: version {args.version} of {target_dir.name}\"")

    print(json.dumps(metadata, indent=2))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        raise SystemExit(str(exc)) from exc
