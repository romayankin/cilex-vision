"""Tests for GET /debug/traces endpoints."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from httpx import AsyncClient


def _make_minio_object(name: str, size: int = 256) -> MagicMock:
    """Create a fake MinIO object listing entry."""
    obj = MagicMock()
    obj.object_name = name
    obj.size = size
    return obj


def _make_minio_response(data: dict) -> MagicMock:
    """Create a fake MinIO get_object response."""
    raw = json.dumps(data).encode()
    resp = MagicMock()
    resp.read.return_value = raw
    return resp


SAMPLE_TRACE = {
    "trace_id": "aaaa-bbbb",
    "frame_id": "f1",
    "camera_id": "cam-1",
    "frame_uri": "s3://bucket/frame.jpg",
    "reason": "sampled",
    "stages": [],
    "detections": [],
    "labels": {},
    "raw_detections_pre_nms": [],
    "tracker_state_delta": {},
    "attribute_outputs": [],
    "model_versions": {"detector": "yolov8l-1"},
    "kafka_offset": 42,
    "source_capture_ts": None,
    "edge_receive_ts": 1000.0,
    "core_ingest_ts": None,
    "track_ids": ["t-1"],
}


class TestListDebugTraces:
    @pytest.mark.asyncio
    async def test_engineering_role_allowed(
        self, client: AsyncClient, make_jwt, app
    ) -> None:
        app.state.minio_client = None
        token = make_jwt(role="engineering")
        resp = await client.get(
            "/debug/traces?camera_id=cam-1",
            cookies={"access_token": token},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["traces"] == []
        assert data["total"] == 0

    @pytest.mark.asyncio
    async def test_admin_role_allowed(
        self, client: AsyncClient, make_jwt, app
    ) -> None:
        app.state.minio_client = None
        token = make_jwt(role="admin")
        resp = await client.get(
            "/debug/traces?camera_id=cam-1",
            cookies={"access_token": token},
        )
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_operator_forbidden(
        self, client: AsyncClient, make_jwt
    ) -> None:
        token = make_jwt(role="operator")
        resp = await client.get(
            "/debug/traces?camera_id=cam-1",
            cookies={"access_token": token},
        )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_viewer_forbidden(
        self, client: AsyncClient, make_jwt
    ) -> None:
        token = make_jwt(role="viewer")
        resp = await client.get(
            "/debug/traces?camera_id=cam-1",
            cookies={"access_token": token},
        )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_camera_id_required(
        self, client: AsyncClient, make_jwt
    ) -> None:
        token = make_jwt(role="engineering")
        resp = await client.get(
            "/debug/traces",
            cookies={"access_token": token},
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_returns_traces_from_minio(
        self, client: AsyncClient, make_jwt, app
    ) -> None:
        mock_minio = MagicMock()
        objs = [
            _make_minio_object("cam-1/2026-04-07/aaaa-bbbb.json", 512),
            _make_minio_object("cam-1/2026-04-07/cccc-dddd.json", 128),
        ]
        mock_minio.list_objects = MagicMock(return_value=iter(objs))
        mock_minio.presigned_get_object = MagicMock(return_value="https://signed-url")
        app.state.minio_client = mock_minio

        token = make_jwt(role="engineering")
        resp = await client.get(
            "/debug/traces?camera_id=cam-1",
            cookies={"access_token": token},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2
        assert len(data["traces"]) == 2
        assert data["traces"][0]["trace_id"] == "aaaa-bbbb"
        assert data["traces"][0]["camera_id"] == "cam-1"
        assert data["traces"][0]["date"] == "2026-04-07"

    @pytest.mark.asyncio
    async def test_date_range_filter(
        self, client: AsyncClient, make_jwt, app
    ) -> None:
        mock_minio = MagicMock()
        objs = [
            _make_minio_object("cam-1/2026-04-05/a.json"),
            _make_minio_object("cam-1/2026-04-07/b.json"),
            _make_minio_object("cam-1/2026-04-09/c.json"),
        ]
        mock_minio.list_objects = MagicMock(return_value=iter(objs))
        mock_minio.presigned_get_object = MagicMock(return_value="https://url")
        app.state.minio_client = mock_minio

        token = make_jwt(role="engineering")
        resp = await client.get(
            "/debug/traces?camera_id=cam-1&start=2026-04-06&end=2026-04-08",
            cookies={"access_token": token},
        )
        data = resp.json()
        assert data["total"] == 1
        assert data["traces"][0]["trace_id"] == "b"


class TestGetDebugTrace:
    @pytest.mark.asyncio
    async def test_fetch_with_date(
        self, client: AsyncClient, make_jwt, app
    ) -> None:
        mock_minio = MagicMock()
        mock_minio.get_object = MagicMock(
            return_value=_make_minio_response(SAMPLE_TRACE)
        )
        app.state.minio_client = mock_minio

        token = make_jwt(role="engineering")
        resp = await client.get(
            "/debug/traces/aaaa-bbbb?camera_id=cam-1&date=2026-04-07",
            cookies={"access_token": token},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["trace_id"] == "aaaa-bbbb"
        assert data["kafka_offset"] == 42
        assert data["model_versions"]["detector"] == "yolov8l-1"

    @pytest.mark.asyncio
    async def test_fetch_without_date_scans(
        self, client: AsyncClient, make_jwt, app
    ) -> None:
        mock_minio = MagicMock()
        objs = [_make_minio_object("cam-1/2026-04-07/aaaa-bbbb.json")]
        mock_minio.list_objects = MagicMock(return_value=iter(objs))
        mock_minio.get_object = MagicMock(
            return_value=_make_minio_response(SAMPLE_TRACE)
        )
        app.state.minio_client = mock_minio

        token = make_jwt(role="engineering")
        resp = await client.get(
            "/debug/traces/aaaa-bbbb?camera_id=cam-1",
            cookies={"access_token": token},
        )
        assert resp.status_code == 200
        assert resp.json()["trace_id"] == "aaaa-bbbb"

    @pytest.mark.asyncio
    async def test_not_found(
        self, client: AsyncClient, make_jwt, app
    ) -> None:
        mock_minio = MagicMock()
        mock_minio.list_objects = MagicMock(return_value=iter([]))
        app.state.minio_client = mock_minio

        token = make_jwt(role="engineering")
        resp = await client.get(
            "/debug/traces/nonexistent?camera_id=cam-1",
            cookies={"access_token": token},
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_minio_unavailable(
        self, client: AsyncClient, make_jwt, app
    ) -> None:
        app.state.minio_client = None
        token = make_jwt(role="engineering")
        resp = await client.get(
            "/debug/traces/aaaa-bbbb?camera_id=cam-1",
            cookies={"access_token": token},
        )
        assert resp.status_code == 503

    @pytest.mark.asyncio
    async def test_operator_forbidden(
        self, client: AsyncClient, make_jwt
    ) -> None:
        token = make_jwt(role="operator")
        resp = await client.get(
            "/debug/traces/aaaa-bbbb?camera_id=cam-1",
            cookies={"access_token": token},
        )
        assert resp.status_code == 403
