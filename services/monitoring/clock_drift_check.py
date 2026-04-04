#!/usr/bin/env python3
"""Emit Prometheus clock skew metrics from Chrony tracking data.

The collector expects a JSON file containing one object per camera:

[
  {
    "camera_id": "cam-001",
    "chrony_host": "edge-01.internal",
    "site_id": "site-a"
  }
]
"""

from __future__ import annotations

import argparse
import csv
import itertools
import json
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence


@dataclass(frozen=True)
class CameraTarget:
    camera_id: str
    chrony_host: str
    site_id: str | None = None


@dataclass(frozen=True)
class TrackingSample:
    chrony_host: str
    source_id: str
    source_name: str
    stratum: int
    ref_time_epoch_seconds: float
    system_time_offset_seconds: float
    last_offset_seconds: float
    rms_offset_seconds: float
    frequency_error_ppm: float
    residual_frequency_ppm: float
    skew_ppm: float
    root_delay_seconds: float
    root_dispersion_seconds: float
    update_interval_seconds: float
    leap_status: str

    @classmethod
    def from_csv_row(cls, chrony_host: str, row: Sequence[str]) -> "TrackingSample":
        if len(row) < 14:
            raise ValueError(
                f"expected at least 14 CSV fields from chronyc tracking for {chrony_host}, got {len(row)}"
            )

        return cls(
            chrony_host=chrony_host,
            source_id=row[0].strip(),
            source_name=row[1].strip(),
            stratum=int(row[2]),
            ref_time_epoch_seconds=float(row[3]),
            system_time_offset_seconds=float(row[4]),
            last_offset_seconds=float(row[5]),
            rms_offset_seconds=float(row[6]),
            frequency_error_ppm=float(row[7]),
            residual_frequency_ppm=float(row[8]),
            skew_ppm=float(row[9]),
            root_delay_seconds=float(row[10]),
            root_dispersion_seconds=float(row[11]),
            update_interval_seconds=float(row[12]),
            leap_status=row[13].strip(),
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Query chronyc tracking on camera time domains and emit clock_skew_ms metrics."
    )
    parser.add_argument(
        "--targets",
        required=True,
        type=Path,
        help="Path to a JSON file with camera_id, chrony_host, and optional site_id entries.",
    )
    parser.add_argument(
        "--chronyc-bin",
        default="chronyc",
        help="Path to the chronyc binary or compatible test double.",
    )
    parser.add_argument(
        "--timeout-seconds",
        default=5.0,
        type=float,
        help="Timeout for each chronyc query.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Optional path to write Prometheus exposition text atomically. Defaults to stdout.",
    )
    return parser.parse_args()


def load_targets(path: Path) -> list[CameraTarget]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"targets file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON in targets file {path}: {exc}") from exc

    if isinstance(payload, dict):
        raw_targets = payload.get("targets")
    else:
        raw_targets = payload

    if not isinstance(raw_targets, list):
        raise ValueError("targets file must be a JSON list or an object with a 'targets' list")
    if not raw_targets:
        raise ValueError("targets file must contain at least one camera target")

    targets: list[CameraTarget] = []
    seen_camera_ids: set[str] = set()

    for index, item in enumerate(raw_targets, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"target #{index} must be an object")

        camera_id = item.get("camera_id")
        chrony_host = item.get("chrony_host")
        site_id = item.get("site_id")

        if not isinstance(camera_id, str) or not camera_id:
            raise ValueError(f"target #{index} has invalid camera_id")
        if not isinstance(chrony_host, str) or not chrony_host:
            raise ValueError(f"target #{index} has invalid chrony_host")
        if site_id is not None and not isinstance(site_id, str):
            raise ValueError(f"target #{index} has invalid site_id")
        if camera_id in seen_camera_ids:
            raise ValueError(f"duplicate camera_id in targets file: {camera_id}")

        seen_camera_ids.add(camera_id)
        targets.append(CameraTarget(camera_id=camera_id, chrony_host=chrony_host, site_id=site_id))

    return sorted(targets, key=lambda target: ((target.site_id or ""), target.camera_id))


def query_tracking(
    chronyc_bin: str,
    chrony_host: str,
    timeout_seconds: float,
) -> TrackingSample:
    command = [chronyc_bin, "-n", "-h", chrony_host, "-c", "tracking"]
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(f"chronyc binary not found: {chronyc_bin}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"chronyc tracking timed out for host {chrony_host}") from exc

    if result.returncode != 0:
        stderr = result.stderr.strip() or "<no stderr>"
        raise RuntimeError(
            f"chronyc tracking failed for host {chrony_host} with exit code {result.returncode}: {stderr}"
        )

    rows = [
        row
        for row in csv.reader(line for line in result.stdout.splitlines() if line.strip())
        if row
    ]
    if not rows:
        raise RuntimeError(f"chronyc tracking returned no CSV rows for host {chrony_host}")

    return TrackingSample.from_csv_row(chrony_host=chrony_host, row=rows[-1])


def collect_tracking_samples(
    targets: Sequence[CameraTarget],
    chronyc_bin: str,
    timeout_seconds: float,
) -> dict[str, TrackingSample]:
    samples: dict[str, TrackingSample] = {}
    for chrony_host in sorted({target.chrony_host for target in targets}):
        samples[chrony_host] = query_tracking(
            chronyc_bin=chronyc_bin,
            chrony_host=chrony_host,
            timeout_seconds=timeout_seconds,
        )
    return samples


def iter_camera_pairs(
    targets: Sequence[CameraTarget],
) -> Iterable[tuple[CameraTarget, CameraTarget]]:
    grouped_targets: dict[str, list[CameraTarget]] = {}
    for target in targets:
        group_key = target.site_id or "__all__"
        grouped_targets.setdefault(group_key, []).append(target)

    for group_targets in grouped_targets.values():
        for pair in itertools.combinations(group_targets, 2):
            yield pair


def compute_clock_skew_ms(
    target_a: CameraTarget,
    target_b: CameraTarget,
    samples: dict[str, TrackingSample],
) -> float:
    offset_a = samples[target_a.chrony_host].system_time_offset_seconds
    offset_b = samples[target_b.chrony_host].system_time_offset_seconds
    return abs(offset_a - offset_b) * 1000.0


def escape_label_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def render_metrics(
    targets: Sequence[CameraTarget],
    samples: dict[str, TrackingSample],
) -> str:
    lines = [
        "# HELP clock_skew_ms Estimated pairwise clock skew between camera time domains.",
        "# TYPE clock_skew_ms gauge",
    ]

    for target_a, target_b in iter_camera_pairs(targets):
        skew_ms = compute_clock_skew_ms(target_a=target_a, target_b=target_b, samples=samples)
        lines.append(
            'clock_skew_ms{camera_a="%s",camera_b="%s"} %.6f'
            % (
                escape_label_value(target_a.camera_id),
                escape_label_value(target_b.camera_id),
                skew_ms,
            )
        )

    return "\n".join(lines) + "\n"


def write_output(metrics_text: str, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=output_path.parent,
        prefix=output_path.name,
        suffix=".tmp",
        delete=False,
    ) as handle:
        handle.write(metrics_text)
        temp_path = Path(handle.name)

    temp_path.replace(output_path)


def main() -> int:
    args = parse_args()

    try:
        targets = load_targets(args.targets)
        samples = collect_tracking_samples(
            targets=targets,
            chronyc_bin=args.chronyc_bin,
            timeout_seconds=args.timeout_seconds,
        )
        metrics_text = render_metrics(targets=targets, samples=samples)

        if args.output is None:
            sys.stdout.write(metrics_text)
        else:
            write_output(metrics_text=metrics_text, output_path=args.output)
    except (RuntimeError, ValueError) as exc:
        print(f"clock_drift_check.py: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
