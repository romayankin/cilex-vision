#!/usr/bin/env python3
"""Generate per-camera calibration trend report from PostgreSQL."""

from __future__ import annotations

import argparse
import asyncio
import importlib
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_OUTPUT_PATH = Path("artifacts/calibration/calibration-report.md")
DEFAULT_INTERVAL_DAYS = 7
DEFAULT_MAX_HISTORY = 5
CREATE_RESULTS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS calibration_results (
    result_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    camera_id TEXT NOT NULL REFERENCES cameras(camera_id),
    calibrated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    pass_through_rate DOUBLE PRECISION NOT NULL,
    miss_rate DOUBLE PRECISION NOT NULL,
    false_trigger_rate DOUBLE PRECISION NOT NULL,
    recommended_pixel_threshold INTEGER,
    recommended_motion_threshold DOUBLE PRECISION,
    recommended_scene_change_threshold DOUBLE PRECISION,
    scorecard_json JSONB,
    status TEXT NOT NULL DEFAULT 'success'
)
"""
CREATE_RESULTS_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_calibration_camera_time
ON calibration_results (camera_id, calibrated_at DESC)
"""


@dataclass(frozen=True)
class CameraRow:
    camera_id: str
    db_site_id: str
    name: str
    status: str


@dataclass(frozen=True)
class CalibrationHistoryEntry:
    camera_id: str
    calibrated_at: datetime
    pass_through_rate: float
    miss_rate: float
    false_trigger_rate: float
    status: str


@dataclass(frozen=True)
class CameraReportRow:
    camera: CameraRow
    latest: CalibrationHistoryEntry | None
    history: list[CalibrationHistoryEntry]


def require_module(module_name: str, install_hint: str) -> Any:
    try:
        return importlib.import_module(module_name)
    except ImportError as exc:  # pragma: no cover - depends on local env
        raise RuntimeError(
            f"missing optional dependency '{module_name}'; install {install_hint}"
        ) from exc


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db-dsn",
        default=os.environ.get("DATABASE_URL") or os.environ.get("DB_DSN"),
        help="PostgreSQL / TimescaleDB DSN.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT_PATH,
        help="Markdown output path.",
    )
    parser.add_argument(
        "--interval-days",
        type=int,
        default=DEFAULT_INTERVAL_DAYS,
        help="Calibration freshness interval used to flag stale cameras.",
    )
    parser.add_argument(
        "--max-history",
        type=int,
        default=DEFAULT_MAX_HISTORY,
        help="Maximum historical calibration points to show per camera.",
    )
    return parser.parse_args()


async def ensure_calibration_results_table(connection: Any) -> None:
    await connection.execute(CREATE_RESULTS_TABLE_SQL)
    await connection.execute(CREATE_RESULTS_INDEX_SQL)


async def fetch_cameras(connection: Any) -> list[CameraRow]:
    rows = await connection.fetch(
        """
        SELECT camera_id, site_id::text AS db_site_id, name, status
        FROM cameras
        ORDER BY camera_id
        """
    )
    return [
        CameraRow(
            camera_id=str(row["camera_id"]),
            db_site_id=str(row["db_site_id"]),
            name=str(row["name"]),
            status=str(row["status"]),
        )
        for row in rows
    ]


async def fetch_calibration_history(
    connection: Any,
) -> dict[str, list[CalibrationHistoryEntry]]:
    rows = await connection.fetch(
        """
        SELECT
            camera_id,
            calibrated_at,
            pass_through_rate,
            miss_rate,
            false_trigger_rate,
            status
        FROM calibration_results
        ORDER BY camera_id, calibrated_at DESC
        """
    )
    history: dict[str, list[CalibrationHistoryEntry]] = {}
    for row in rows:
        entry = CalibrationHistoryEntry(
            camera_id=str(row["camera_id"]),
            calibrated_at=row["calibrated_at"],
            pass_through_rate=float(row["pass_through_rate"]),
            miss_rate=float(row["miss_rate"]),
            false_trigger_rate=float(row["false_trigger_rate"]),
            status=str(row["status"]),
        )
        history.setdefault(entry.camera_id, []).append(entry)
    return history


def build_report_rows(
    cameras: list[CameraRow],
    history_by_camera: dict[str, list[CalibrationHistoryEntry]],
    *,
    max_history: int,
) -> list[CameraReportRow]:
    rows: list[CameraReportRow] = []
    for camera in cameras:
        history = history_by_camera.get(camera.camera_id, [])[:max_history]
        latest = history[0] if history else None
        rows.append(CameraReportRow(camera=camera, latest=latest, history=history))
    return rows


def format_datetime(value: datetime | None) -> str:
    if value is None:
        return "never"
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def format_rate(value: float | None) -> str:
    if value is None:
        return "n/a"
    return f"{value:.4f}"


def format_days_since(value: float | None) -> str:
    if value is None:
        return "never"
    return f"{value:.2f}"


def days_since(value: datetime | None, *, now: datetime) -> float | None:
    if value is None:
        return None
    return max((now - value).total_seconds(), 0.0) / 86400.0


def build_markdown_report(
    *,
    rows: list[CameraReportRow],
    interval_days: int,
    generated_at: datetime,
) -> str:
    stale_rows = [
        row
        for row in rows
        if row.camera.status == "online"
        and (
            row.latest is None
            or (days_since(row.latest.calibrated_at, now=generated_at) or 0.0) > interval_days
        )
    ]
    latest_rates = [
        row.latest.pass_through_rate
        for row in rows
        if row.latest is not None
    ]
    average_pass_through = (
        sum(latest_rates) / len(latest_rates)
        if latest_rates
        else None
    )

    lines = [
        "# Calibration Trend Report",
        "",
        f"- Generated at: `{format_datetime(generated_at)}`",
        f"- Freshness interval: `{interval_days}` days",
        f"- Total cameras: `{len(rows)}`",
        f"- Cameras with calibration history: `{sum(1 for row in rows if row.latest is not None)}`",
        f"- Stale online cameras: `{len(stale_rows)}`",
        f"- Average latest pass-through rate: `{format_rate(average_pass_through)}`",
        "",
        "## Per-Camera Status",
        "",
        "| Camera ID | Name | Status | Last calibrated | Pass-through | Miss rate | False trigger rate | Days since calibration |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        latest = row.latest
        age = days_since(latest.calibrated_at, now=generated_at) if latest is not None else None
        lines.append(
            "| "
            + " | ".join(
                [
                    row.camera.camera_id,
                    row.camera.name,
                    row.camera.status,
                    format_datetime(latest.calibrated_at if latest is not None else None),
                    format_rate(latest.pass_through_rate if latest is not None else None),
                    format_rate(latest.miss_rate if latest is not None else None),
                    format_rate(latest.false_trigger_rate if latest is not None else None),
                    format_days_since(age),
                ]
            )
            + " |"
        )

    lines.extend(
        [
            "",
            "## Trend History",
            "",
            "Newest calibration appears first for each camera.",
            "",
        ]
    )
    for row in rows:
        lines.append(f"### {row.camera.camera_id} — {row.camera.name}")
        if not row.history:
            lines.extend(["", "- No calibration history recorded.", ""])
            continue
        lines.extend(
            [
                "",
                "| Calibrated at | Pass-through | Miss rate | False trigger rate | Status |",
                "| --- | --- | --- | --- | --- |",
            ]
        )
        for entry in row.history:
            lines.append(
                "| "
                + " | ".join(
                    [
                        format_datetime(entry.calibrated_at),
                        format_rate(entry.pass_through_rate),
                        format_rate(entry.miss_rate),
                        format_rate(entry.false_trigger_rate),
                        entry.status,
                    ]
                )
                + " |"
            )
        lines.append("")

    lines.extend(["## Stale Cameras", ""])
    if not stale_rows:
        lines.append(f"- No online cameras are older than `{interval_days}` days.")
    else:
        for row in stale_rows:
            latest_text = (
                format_datetime(row.latest.calibrated_at)
                if row.latest is not None
                else "never calibrated"
            )
            lines.append(
                f"- `{row.camera.camera_id}` ({row.camera.name}) — last calibration: {latest_text}"
            )

    lines.extend(
        [
            "",
            "## Summary",
            "",
            f"- Total cameras in DB: `{len(rows)}`",
            f"- Cameras with at least one calibration: `{sum(1 for row in rows if row.latest is not None)}`",
            f"- Online stale cameras: `{len(stale_rows)}`",
            f"- Average latest pass-through rate: `{format_rate(average_pass_through)}`",
        ]
    )
    return "\n".join(lines) + "\n"


async def generate_report(args: argparse.Namespace) -> Path:
    if not args.db_dsn:
        raise RuntimeError("--db-dsn is required")
    if args.max_history <= 0:
        raise RuntimeError("--max-history must be greater than zero")
    if args.interval_days <= 0:
        raise RuntimeError("--interval-days must be greater than zero")

    asyncpg = require_module("asyncpg", "asyncpg")
    connection = await asyncpg.connect(args.db_dsn)
    try:
        await ensure_calibration_results_table(connection)
        cameras = await fetch_cameras(connection)
        history_by_camera = await fetch_calibration_history(connection)
    finally:
        await connection.close()

    generated_at = datetime.now(tz=timezone.utc)
    rows = build_report_rows(
        cameras,
        history_by_camera,
        max_history=args.max_history,
    )
    report = build_markdown_report(
        rows=rows,
        interval_days=args.interval_days,
        generated_at=generated_at,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(report, encoding="utf-8")
    return args.output


def main() -> None:
    args = parse_args()
    output_path = asyncio.run(generate_report(args))
    print(output_path)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:  # pragma: no cover - CLI boundary
        raise SystemExit(130) from None
    except Exception as exc:  # pragma: no cover - CLI boundary
        raise SystemExit(str(exc)) from exc
