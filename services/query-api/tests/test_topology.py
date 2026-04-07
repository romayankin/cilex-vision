"""Tests for the topology router registered in query-api."""

from __future__ import annotations

import pytest
from httpx import AsyncClient


def _camera_row(
    camera_id: str = "cam-1",
    site_id: str = "site-1",
) -> dict:
    return {
        "camera_id": camera_id,
        "site_id": site_id,
        "name": "Test Camera",
        "latitude": 40.0,
        "longitude": -74.0,
        "status": "online",
        "location_description": "Entrance",
        "config_json": '{"zone_id": "zone-a"}',
    }


def _edge_row(
    edge_id: str = "edge-1",
    camera_a: str = "cam-1",
    camera_b: str = "cam-2",
) -> dict:
    return {
        "edge_id": edge_id,
        "camera_a_id": camera_a,
        "camera_b_id": camera_b,
        "transition_time_s": 15.0,
        "confidence": 0.9,
        "enabled": True,
    }


class TestGetTopology:

    @pytest.mark.asyncio
    async def test_returns_topology_graph(
        self, client: AsyncClient, make_jwt, fake_pool
    ) -> None:
        fake_pool.set_rows_sequence([
            [_camera_row("cam-1"), _camera_row("cam-2")],
            [_edge_row()],
        ])
        token = make_jwt(role="operator")
        resp = await client.get(
            "/topology/site-1",
            cookies={"access_token": token},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["site_id"] == "site-1"
        assert len(data["cameras"]) == 2
        assert len(data["edges"]) == 1
        assert data["cameras"][0]["camera_id"] == "cam-1"
        assert data["cameras"][0]["zone_id"] == "zone-a"

    @pytest.mark.asyncio
    async def test_empty_site(
        self, client: AsyncClient, make_jwt, fake_pool
    ) -> None:
        fake_pool.set_rows_sequence([[], []])
        token = make_jwt(role="operator")
        resp = await client.get(
            "/topology/empty-site",
            cookies={"access_token": token},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["cameras"] == []
        assert data["edges"] == []

    @pytest.mark.asyncio
    async def test_requires_auth(self, client: AsyncClient) -> None:
        resp = await client.get("/topology/site-1")
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_viewer_forbidden(
        self, client: AsyncClient, make_jwt, fake_pool
    ) -> None:
        token = make_jwt(role="viewer")
        resp = await client.get(
            "/topology/site-1",
            cookies={"access_token": token},
        )
        assert resp.status_code == 403
