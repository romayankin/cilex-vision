"""Tests for GET /detections endpoint."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from httpx import AsyncClient


def _detection_row(
    camera_id: str = "cam-1",
    object_class: str = "person",
    confidence: float = 0.85,
) -> dict:
    return {
        "time": datetime(2026, 4, 7, 10, 0, 0, tzinfo=timezone.utc),
        "camera_id": camera_id,
        "frame_seq": 42,
        "object_class": object_class,
        "confidence": confidence,
        "bbox_x": 0.1,
        "bbox_y": 0.2,
        "bbox_w": 0.3,
        "bbox_h": 0.4,
        "local_track_id": uuid.uuid4(),
        "model_version": "yolov8l-1",
    }


class TestListDetections:
    @pytest.mark.asyncio
    async def test_empty_result(
        self, client: AsyncClient, make_jwt, fake_pool
    ) -> None:
        fake_pool.set_count(0)
        fake_pool.set_rows([])
        token = make_jwt(role="operator")
        resp = await client.get(
            "/detections",
            cookies={"access_token": token},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["detections"] == []
        assert data["total"] == 0
        assert data["offset"] == 0
        assert data["limit"] == 50

    @pytest.mark.asyncio
    async def test_returns_detections(
        self, client: AsyncClient, make_jwt, fake_pool
    ) -> None:
        fake_pool.set_count(2)
        fake_pool.set_rows([
            _detection_row(camera_id="cam-1"),
            _detection_row(camera_id="cam-2", object_class="car"),
        ])
        token = make_jwt(role="operator")
        resp = await client.get(
            "/detections",
            cookies={"access_token": token},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["detections"]) == 2
        assert data["total"] == 2

    @pytest.mark.asyncio
    async def test_detection_fields(
        self, client: AsyncClient, make_jwt, fake_pool
    ) -> None:
        fake_pool.set_count(1)
        fake_pool.set_rows([_detection_row()])
        token = make_jwt(role="operator")
        resp = await client.get(
            "/detections",
            cookies={"access_token": token},
        )
        det = resp.json()["detections"][0]
        assert det["camera_id"] == "cam-1"
        assert det["object_class"] == "person"
        assert det["confidence"] == 0.85
        assert det["bbox"]["x"] == 0.1
        assert det["bbox"]["w"] == 0.3
        assert det["model_version"] == "yolov8l-1"

    @pytest.mark.asyncio
    async def test_pagination_params(
        self, client: AsyncClient, make_jwt, fake_pool
    ) -> None:
        fake_pool.set_count(100)
        fake_pool.set_rows([_detection_row()])
        token = make_jwt(role="operator")
        resp = await client.get(
            "/detections?offset=10&limit=5",
            cookies={"access_token": token},
        )
        data = resp.json()
        assert data["offset"] == 10
        assert data["limit"] == 5

    @pytest.mark.asyncio
    async def test_limit_validation(
        self, client: AsyncClient, make_jwt
    ) -> None:
        token = make_jwt(role="operator")
        resp = await client.get(
            "/detections?limit=5000",
            cookies={"access_token": token},
        )
        assert resp.status_code == 422  # validation error

    @pytest.mark.asyncio
    async def test_min_confidence_filter(
        self, client: AsyncClient, make_jwt, fake_pool
    ) -> None:
        fake_pool.set_count(1)
        fake_pool.set_rows([_detection_row(confidence=0.9)])
        token = make_jwt(role="operator")
        resp = await client.get(
            "/detections?min_confidence=0.8",
            cookies={"access_token": token},
        )
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_class_filter(
        self, client: AsyncClient, make_jwt, fake_pool
    ) -> None:
        fake_pool.set_count(1)
        fake_pool.set_rows([_detection_row(object_class="car")])
        token = make_jwt(role="operator")
        resp = await client.get(
            "/detections?class=car",
            cookies={"access_token": token},
        )
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_null_track_id(
        self, client: AsyncClient, make_jwt, fake_pool
    ) -> None:
        row = _detection_row()
        row["local_track_id"] = None
        fake_pool.set_count(1)
        fake_pool.set_rows([row])
        token = make_jwt(role="operator")
        resp = await client.get(
            "/detections",
            cookies={"access_token": token},
        )
        det = resp.json()["detections"][0]
        assert det["local_track_id"] is None
