"""Tests for GET /events endpoint."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from httpx import AsyncClient


def _event_row(
    camera_id: str = "cam-1",
    event_type: str = "entered_scene",
    state: str = "closed",
) -> dict:
    return {
        "event_id": uuid.uuid4(),
        "event_type": event_type,
        "track_id": uuid.uuid4(),
        "camera_id": camera_id,
        "start_time": datetime(2026, 4, 7, 10, 0, 0, tzinfo=timezone.utc),
        "end_time": datetime(2026, 4, 7, 10, 0, 0, tzinfo=timezone.utc),
        "duration_ms": 0,
        "clip_uri": "s3://event-clips/cam-1/2026-04-07/clip.mp4",
        "state": state,
        "metadata_jsonb": {"zone": "entrance"},
        "source_capture_ts": datetime(2026, 4, 7, 9, 59, 59, tzinfo=timezone.utc),
        "edge_receive_ts": datetime(2026, 4, 7, 10, 0, 0, tzinfo=timezone.utc),
        "core_ingest_ts": datetime(2026, 4, 7, 10, 0, 1, tzinfo=timezone.utc),
    }


class TestListEvents:
    @pytest.mark.asyncio
    async def test_empty_result(
        self, client: AsyncClient, make_jwt, fake_pool
    ) -> None:
        fake_pool.set_count(0)
        fake_pool.set_rows([])
        token = make_jwt(role="operator")
        resp = await client.get(
            "/events",
            cookies={"access_token": token},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["events"] == []
        assert data["total"] == 0

    @pytest.mark.asyncio
    async def test_returns_events(
        self, client: AsyncClient, make_jwt, fake_pool
    ) -> None:
        fake_pool.set_count(1)
        fake_pool.set_rows([_event_row()])
        token = make_jwt(role="operator")
        resp = await client.get(
            "/events",
            cookies={"access_token": token},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["events"]) == 1

    @pytest.mark.asyncio
    async def test_event_fields(
        self, client: AsyncClient, make_jwt, fake_pool
    ) -> None:
        fake_pool.set_count(1)
        fake_pool.set_rows([_event_row()])
        token = make_jwt(role="operator")
        resp = await client.get(
            "/events",
            cookies={"access_token": token},
        )
        event = resp.json()["events"][0]
        assert event["event_type"] == "entered_scene"
        assert event["camera_id"] == "cam-1"
        assert event["state"] == "closed"
        assert event["duration_ms"] == 0
        assert event["metadata"] == {"zone": "entrance"}

    @pytest.mark.asyncio
    async def test_clip_url_none_when_no_minio(
        self, client: AsyncClient, make_jwt, fake_pool
    ) -> None:
        # MinIO client is None in test fixtures, so clip_url should be None
        fake_pool.set_count(1)
        fake_pool.set_rows([_event_row()])
        token = make_jwt(role="operator")
        resp = await client.get(
            "/events",
            cookies={"access_token": token},
        )
        event = resp.json()["events"][0]
        assert event["clip_url"] is None  # no minio client

    @pytest.mark.asyncio
    async def test_event_type_filter(
        self, client: AsyncClient, make_jwt, fake_pool
    ) -> None:
        fake_pool.set_count(1)
        fake_pool.set_rows([_event_row(event_type="loitering")])
        token = make_jwt(role="operator")
        resp = await client.get(
            "/events?event_type=loitering",
            cookies={"access_token": token},
        )
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_null_clip_uri(
        self, client: AsyncClient, make_jwt, fake_pool
    ) -> None:
        row = _event_row()
        row["clip_uri"] = None
        fake_pool.set_count(1)
        fake_pool.set_rows([row])
        token = make_jwt(role="operator")
        resp = await client.get(
            "/events",
            cookies={"access_token": token},
        )
        event = resp.json()["events"][0]
        assert event["clip_url"] is None

    @pytest.mark.asyncio
    async def test_null_track_id(
        self, client: AsyncClient, make_jwt, fake_pool
    ) -> None:
        row = _event_row()
        row["track_id"] = None
        fake_pool.set_count(1)
        fake_pool.set_rows([row])
        token = make_jwt(role="operator")
        resp = await client.get(
            "/events",
            cookies={"access_token": token},
        )
        event = resp.json()["events"][0]
        assert event["track_id"] is None

    @pytest.mark.asyncio
    async def test_engineering_cannot_access_events(
        self, client: AsyncClient, make_jwt
    ) -> None:
        token = make_jwt(role="engineering")
        resp = await client.get(
            "/events",
            cookies={"access_token": token},
        )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_timestamps_present(
        self, client: AsyncClient, make_jwt, fake_pool
    ) -> None:
        fake_pool.set_count(1)
        fake_pool.set_rows([_event_row()])
        token = make_jwt(role="operator")
        resp = await client.get(
            "/events",
            cookies={"access_token": token},
        )
        event = resp.json()["events"][0]
        assert event["source_capture_ts"] is not None
        assert event["edge_receive_ts"] is not None
        assert event["core_ingest_ts"] is not None
