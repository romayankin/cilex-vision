"""Tests for GET /tracks and GET /tracks/{id} endpoints."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from httpx import AsyncClient


def _track_row(
    camera_id: str = "cam-1",
    object_class: str = "person",
    state: str = "active",
) -> dict:
    return {
        "local_track_id": uuid.uuid4(),
        "camera_id": camera_id,
        "object_class": object_class,
        "state": state,
        "mean_confidence": 0.82,
        "start_time": datetime(2026, 4, 7, 9, 0, 0, tzinfo=timezone.utc),
        "end_time": None,
        "tracker_version": "bytetrack-1.0",
        "created_at": datetime(2026, 4, 7, 9, 0, 0, tzinfo=timezone.utc),
    }


def _attr_row() -> dict:
    return {
        "attribute_id": uuid.uuid4(),
        "attribute_type": "vehicle_color",
        "color_value": "red",
        "confidence": 0.75,
        "model_version": "color-v1",
        "observed_at": datetime(2026, 4, 7, 9, 5, 0, tzinfo=timezone.utc),
    }


class TestListTracks:
    @pytest.mark.asyncio
    async def test_empty_result(
        self, client: AsyncClient, make_jwt, fake_pool
    ) -> None:
        fake_pool.set_count(0)
        fake_pool.set_rows([])
        token = make_jwt(role="operator")
        resp = await client.get(
            "/tracks",
            cookies={"access_token": token},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["tracks"] == []
        assert data["total"] == 0

    @pytest.mark.asyncio
    async def test_returns_tracks(
        self, client: AsyncClient, make_jwt, fake_pool
    ) -> None:
        fake_pool.set_count(1)
        fake_pool.set_rows([_track_row()])
        token = make_jwt(role="operator")
        resp = await client.get(
            "/tracks",
            cookies={"access_token": token},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["tracks"]) == 1

    @pytest.mark.asyncio
    async def test_track_fields(
        self, client: AsyncClient, make_jwt, fake_pool
    ) -> None:
        fake_pool.set_count(1)
        fake_pool.set_rows([_track_row()])
        token = make_jwt(role="operator")
        resp = await client.get(
            "/tracks",
            cookies={"access_token": token},
        )
        track = resp.json()["tracks"][0]
        assert track["camera_id"] == "cam-1"
        assert track["object_class"] == "person"
        assert track["state"] == "active"
        assert track["mean_confidence"] == 0.82
        assert track["tracker_version"] == "bytetrack-1.0"

    @pytest.mark.asyncio
    async def test_state_filter(
        self, client: AsyncClient, make_jwt, fake_pool
    ) -> None:
        fake_pool.set_count(1)
        fake_pool.set_rows([_track_row(state="terminated")])
        token = make_jwt(role="operator")
        resp = await client.get(
            "/tracks?state=terminated",
            cookies={"access_token": token},
        )
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_class_filter(
        self, client: AsyncClient, make_jwt, fake_pool
    ) -> None:
        fake_pool.set_count(1)
        fake_pool.set_rows([_track_row(object_class="car")])
        token = make_jwt(role="operator")
        resp = await client.get(
            "/tracks?class=car",
            cookies={"access_token": token},
        )
        assert resp.status_code == 200


class TestTrackDetail:
    @pytest.mark.asyncio
    async def test_not_found(
        self, client: AsyncClient, make_jwt, fake_pool
    ) -> None:
        fake_pool.set_count(0)
        fake_pool.set_rows([])
        token = make_jwt(role="operator")
        track_id = str(uuid.uuid4())
        resp = await client.get(
            f"/tracks/{track_id}",
            cookies={"access_token": token},
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_returns_detail_with_attributes(
        self, client: AsyncClient, make_jwt, fake_pool
    ) -> None:
        track = _track_row(camera_id="cam-1")
        attr = _attr_row()
        # First fetch → track row, second fetch → attribute rows
        fake_pool.set_rows_sequence([[track], [attr]])
        token = make_jwt(role="operator", camera_scope=["cam-1"])
        track_id = str(track["local_track_id"])
        resp = await client.get(
            f"/tracks/{track_id}",
            cookies={"access_token": token},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["camera_id"] == "cam-1"
        assert data["object_class"] == "person"
        assert len(data["attributes"]) == 1
        assert data["attributes"][0]["color_value"] == "red"

    @pytest.mark.asyncio
    async def test_camera_scope_blocks_access(
        self, client: AsyncClient, make_jwt, fake_pool
    ) -> None:
        track = _track_row(camera_id="cam-999")
        fake_pool.set_rows_sequence([[track]])
        # User only has access to cam-1 and cam-2
        token = make_jwt(role="operator", camera_scope=["cam-1", "cam-2"])
        track_id = str(track["local_track_id"])
        resp = await client.get(
            f"/tracks/{track_id}",
            cookies={"access_token": token},
        )
        assert resp.status_code == 404  # scoped out

    @pytest.mark.asyncio
    async def test_admin_bypasses_scope(
        self, client: AsyncClient, make_jwt, fake_pool
    ) -> None:
        track = _track_row(camera_id="cam-999")
        # First fetch → track row, second fetch → empty attributes
        fake_pool.set_rows_sequence([[track], []])
        token = make_jwt(role="admin", camera_scope=[])
        track_id = str(track["local_track_id"])
        resp = await client.get(
            f"/tracks/{track_id}",
            cookies={"access_token": token},
        )
        assert resp.status_code == 200  # admin sees everything
