#!/usr/bin/env python3
"""Fetch the currently available journey context for a local track.

The Query API does not yet expose MTMC global-track links directly. This
example therefore combines:

- GET /tracks/{local_track_id}
- GET /events in the track's time window
- optional GET /topology/{site_id} for adjacent-camera context
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("local_track_id", help="Local track UUID to inspect.")
    parser.add_argument(
        "--base-url",
        default=os.environ.get("BASE_URL", "http://localhost:8000"),
        help="Query API base URL.",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("TOKEN"),
        help="JWT value for the access_token cookie. Defaults to TOKEN env var.",
    )
    parser.add_argument(
        "--site-id",
        default=os.environ.get("SITE_ID"),
        help="Optional site ID for topology context.",
    )
    parser.add_argument(
        "--pre-roll-s",
        type=int,
        default=5,
        help="Seconds before track start to include in the event lookup window.",
    )
    parser.add_argument(
        "--post-roll-s",
        type=int,
        default=5,
        help="Seconds after track end to include in the event lookup window.",
    )
    parser.add_argument(
        "--event-limit",
        type=int,
        default=25,
        help="Maximum number of related events to fetch.",
    )
    return parser.parse_args()


def _parse_iso8601(value: str) -> datetime:
    normalized = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _iso8601(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _get_json(
    client: httpx.Client,
    path: str,
    params: dict[str, Any] | None = None,
) -> Any:
    response = client.get(path, params=params)
    response.raise_for_status()
    return response.json()


def _adjacent_cameras(topology: dict[str, Any], camera_id: str) -> list[str]:
    neighbors: set[str] = set()
    for edge in topology.get("edges", []):
        if not edge.get("enabled", True):
            continue
        if edge.get("camera_a_id") == camera_id:
            neighbors.add(str(edge["camera_b_id"]))
        elif edge.get("camera_b_id") == camera_id:
            neighbors.add(str(edge["camera_a_id"]))
    return sorted(neighbors)


def main() -> None:
    args = _parse_args()
    if not args.token:
        raise SystemExit("missing token: set TOKEN or pass --token")

    cookies = {"access_token": args.token}
    with httpx.Client(base_url=args.base_url, cookies=cookies, timeout=30.0) as client:
        try:
            track = _get_json(client, f"/tracks/{args.local_track_id}")
        except httpx.HTTPError as exc:
            raise SystemExit(f"failed to fetch track: {exc}") from exc

        start_time = _parse_iso8601(track["start_time"]) - timedelta(seconds=args.pre_roll_s)
        track_end = track.get("end_time") or track["start_time"]
        end_time = _parse_iso8601(track_end) + timedelta(seconds=args.post_roll_s)

        try:
            events = _get_json(
                client,
                "/events",
                params={
                    "camera_id": track["camera_id"],
                    "start": _iso8601(start_time),
                    "end": _iso8601(end_time),
                    "limit": args.event_limit,
                },
            )
        except httpx.HTTPError as exc:
            raise SystemExit(f"failed to fetch related events: {exc}") from exc

        result: dict[str, Any] = {
            "track": track,
            "related_events": events.get("events", []),
        }

        if args.site_id:
            try:
                topology = _get_json(client, f"/topology/{args.site_id}")
            except httpx.HTTPError as exc:
                raise SystemExit(f"failed to fetch topology context: {exc}") from exc

            result["topology_context"] = {
                "site_id": args.site_id,
                "adjacent_cameras": _adjacent_cameras(topology, str(track["camera_id"])),
            }

        print(json.dumps(result, indent=2, sort_keys=False))


if __name__ == "__main__":
    main()
