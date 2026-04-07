"""Tests for the camera topology service.

Covers:
- Pydantic model construction and validation
- Graph helpers: adjacent_cameras, downstream_cameras
- API CRUD operations with FakePool
- Seed data validity
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import pytest
from httpx import AsyncClient

from models import (
    OBJECT_CLASSES,
    CameraNode,
    TopologyGraph,
    TransitionEdge,
    TransitTimeDistribution,
)
from seed import CAMERAS, EDGES, build_topology


# ---------------------------------------------------------------
# Model construction
# ---------------------------------------------------------------


class TestTransitTimeDistribution:
    def test_basic_construction(self) -> None:
        d = TransitTimeDistribution(
            object_class="person", p50_ms=5000, p90_ms=7500, p99_ms=12500
        )
        assert d.object_class == "person"
        assert d.sample_count == 0
        assert d.last_updated is None

    def test_with_sample_count(self) -> None:
        d = TransitTimeDistribution(
            object_class="car",
            p50_ms=1500,
            p90_ms=2250,
            p99_ms=3750,
            sample_count=42,
            last_updated=datetime(2026, 4, 7, tzinfo=timezone.utc),
        )
        assert d.sample_count == 42


class TestCameraNode:
    def test_basic_camera(self) -> None:
        cam = CameraNode(
            camera_id="cam-1",
            site_id="site-1",
            name="Test Camera",
            zone_id="lobby",
        )
        assert cam.zone_id == "lobby"
        assert cam.status == "offline"

    def test_optional_fields(self) -> None:
        cam = CameraNode(camera_id="c", site_id="s", name="n")
        assert cam.zone_id is None
        assert cam.latitude is None


class TestTransitionEdge:
    def test_default_distributions(self) -> None:
        dists = TransitionEdge.default_distributions(10.0)
        assert len(dists) == 7
        classes = {d.object_class for d in dists}
        assert classes == set(OBJECT_CLASSES)

        # Person baseline: 10s * 1.0 * 1000 = 10000 ms
        person = next(d for d in dists if d.object_class == "person")
        assert person.p50_ms == 10000.0
        assert person.p90_ms == 15000.0
        assert person.p99_ms == 25000.0

        # Car should be faster (0.3x)
        car = next(d for d in dists if d.object_class == "car")
        assert car.p50_ms == 3000.0

    def test_edge_with_distributions(self) -> None:
        edge = TransitionEdge(
            camera_a_id="a",
            camera_b_id="b",
            transition_time_s=5.0,
            transit_distributions=TransitionEdge.default_distributions(5.0),
        )
        assert len(edge.transit_distributions) == 7


# ---------------------------------------------------------------
# Graph helpers
# ---------------------------------------------------------------


def _sample_graph() -> TopologyGraph:
    """Build a small graph: A → B → C, A → C (long path)."""
    return TopologyGraph(
        site_id="site-1",
        cameras=[
            CameraNode(camera_id="A", site_id="site-1", name="A"),
            CameraNode(camera_id="B", site_id="site-1", name="B"),
            CameraNode(camera_id="C", site_id="site-1", name="C"),
            CameraNode(camera_id="D", site_id="site-1", name="D"),
        ],
        edges=[
            TransitionEdge(
                camera_a_id="A",
                camera_b_id="B",
                transition_time_s=5.0,
                transit_distributions=TransitionEdge.default_distributions(5.0),
            ),
            TransitionEdge(
                camera_a_id="B",
                camera_b_id="C",
                transition_time_s=10.0,
                transit_distributions=TransitionEdge.default_distributions(10.0),
            ),
            TransitionEdge(
                camera_a_id="A",
                camera_b_id="C",
                transition_time_s=30.0,
                transit_distributions=TransitionEdge.default_distributions(30.0),
            ),
            # D is disconnected (disabled edge)
            TransitionEdge(
                camera_a_id="C",
                camera_b_id="D",
                transition_time_s=2.0,
                enabled=False,
                transit_distributions=TransitionEdge.default_distributions(2.0),
            ),
        ],
    )


class TestAdjacentCameras:
    def test_direct_neighbors(self) -> None:
        g = _sample_graph()
        adj = g.adjacent_cameras("A")
        assert set(adj) == {"B", "C"}

    def test_bidirectional(self) -> None:
        g = _sample_graph()
        adj = g.adjacent_cameras("B")
        assert set(adj) == {"A", "C"}

    def test_no_neighbors(self) -> None:
        g = _sample_graph()
        # D has no enabled edges
        adj = g.adjacent_cameras("D")
        assert adj == []

    def test_unknown_camera(self) -> None:
        g = _sample_graph()
        adj = g.adjacent_cameras("nonexistent")
        assert adj == []


class TestDownstreamCameras:
    def test_within_window(self) -> None:
        g = _sample_graph()
        # Person p99 for A→B: 5 * 1.0 * 2.5 * 1000 = 12500 ms
        # Person p99 for B→C: 10 * 1.0 * 2.5 * 1000 = 25000 ms
        # Total A→B→C = 37500 ms
        downstream = g.downstream_cameras("A", 50000.0, "person")
        assert set(downstream) == {"B", "C"}

    def test_window_too_small(self) -> None:
        g = _sample_graph()
        # Only 5000 ms — only A→B p99 = 12500 ms won't fit
        downstream = g.downstream_cameras("A", 5000.0, "person")
        assert downstream == []

    def test_car_is_faster(self) -> None:
        g = _sample_graph()
        # Car factor = 0.3, so A→B p99 = 5 * 0.3 * 2.5 * 1000 = 3750 ms
        downstream = g.downstream_cameras("A", 5000.0, "car")
        assert "B" in downstream

    def test_disabled_edge_excluded(self) -> None:
        g = _sample_graph()
        # C→D is disabled, so D should never appear
        downstream = g.downstream_cameras("A", 1000000.0, "person")
        assert "D" not in downstream

    def test_source_not_in_result(self) -> None:
        g = _sample_graph()
        downstream = g.downstream_cameras("A", 1000000.0, "person")
        assert "A" not in downstream


# ---------------------------------------------------------------
# Seed data validation
# ---------------------------------------------------------------


class TestSeedData:
    def test_four_cameras(self) -> None:
        assert len(CAMERAS) == 4

    def test_cameras_have_zone_ids(self) -> None:
        for cam in CAMERAS:
            assert cam.zone_id is not None, f"{cam.camera_id} missing zone_id"

    def test_edges_have_distributions(self) -> None:
        for edge in EDGES:
            assert len(edge.transit_distributions) == 7

    def test_transit_times_in_range(self) -> None:
        for edge in EDGES:
            # Person p50 should be 1s - 120s (in ms: 1000 - 120000)
            person = next(
                d for d in edge.transit_distributions if d.object_class == "person"
            )
            assert 1000 <= person.p50_ms <= 120000

    def test_build_topology_valid(self) -> None:
        topo = build_topology()
        assert topo.site_id is not None
        assert len(topo.cameras) == 4
        assert len(topo.edges) >= 4

    def test_seed_json_serializable(self) -> None:
        topo = build_topology()
        json_str = topo.model_dump_json()
        parsed = json.loads(json_str)
        assert "cameras" in parsed
        assert "edges" in parsed

    def test_all_edge_cameras_exist(self) -> None:
        topo = build_topology()
        cam_ids = {c.camera_id for c in topo.cameras}
        for edge in topo.edges:
            assert edge.camera_a_id in cam_ids
            assert edge.camera_b_id in cam_ids

    def test_adjacency_from_seed(self) -> None:
        topo = build_topology()
        adj = topo.adjacent_cameras("cam-entrance")
        assert "cam-lobby" in adj

    def test_downstream_from_entrance(self) -> None:
        topo = build_topology()
        # With a large window, all cameras should be reachable from entrance
        downstream = topo.downstream_cameras("cam-entrance", 500000.0, "person")
        assert len(downstream) >= 3


# ---------------------------------------------------------------
# API CRUD tests
# ---------------------------------------------------------------


def _cam_row(
    camera_id: str = "cam-1",
    site_id: str = "site-1",
    zone_id: str | None = "zone-a",
) -> dict:
    config = {"zone_id": zone_id} if zone_id else None
    return {
        "camera_id": camera_id,
        "site_id": site_id,
        "name": f"Camera {camera_id}",
        "latitude": 40.71,
        "longitude": -74.00,
        "status": "online",
        "location_description": "test location",
        "config_json": config,
    }


def _edge_row(
    camera_a: str = "cam-1",
    camera_b: str = "cam-2",
    time_s: float = 10.0,
) -> dict:
    return {
        "edge_id": uuid.uuid4(),
        "camera_a_id": camera_a,
        "camera_b_id": camera_b,
        "transition_time_s": time_s,
        "confidence": 0.9,
        "enabled": True,
    }


class TestGetTopologyAPI:
    @pytest.mark.asyncio
    async def test_returns_graph(
        self, client: AsyncClient, make_jwt, fake_pool
    ) -> None:
        fake_pool.set_rows_sequence([
            [_cam_row("cam-1"), _cam_row("cam-2")],
            [_edge_row("cam-1", "cam-2")],
        ])
        token = make_jwt(role="admin")
        resp = await client.get(
            "/topology/site-1",
            cookies={"access_token": token},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["cameras"]) == 2
        assert len(data["edges"]) == 1
        assert data["edges"][0]["camera_a_id"] == "cam-1"

    @pytest.mark.asyncio
    async def test_cameras_have_zone_id(
        self, client: AsyncClient, make_jwt, fake_pool
    ) -> None:
        fake_pool.set_rows_sequence([
            [_cam_row("cam-1", zone_id="entrance")],
            [],
        ])
        token = make_jwt(role="operator")
        resp = await client.get(
            "/topology/site-1",
            cookies={"access_token": token},
        )
        assert resp.status_code == 200
        assert resp.json()["cameras"][0]["zone_id"] == "entrance"

    @pytest.mark.asyncio
    async def test_edges_have_distributions(
        self, client: AsyncClient, make_jwt, fake_pool
    ) -> None:
        fake_pool.set_rows_sequence([
            [],
            [_edge_row("cam-1", "cam-2", 15.0)],
        ])
        token = make_jwt(role="admin")
        resp = await client.get(
            "/topology/site-1",
            cookies={"access_token": token},
        )
        edge = resp.json()["edges"][0]
        assert len(edge["transit_distributions"]) == 7
        person_dist = next(
            d for d in edge["transit_distributions"] if d["object_class"] == "person"
        )
        assert person_dist["p50_ms"] == 15000.0

    @pytest.mark.asyncio
    async def test_viewer_forbidden(
        self, client: AsyncClient, make_jwt
    ) -> None:
        token = make_jwt(role="viewer")
        resp = await client.get(
            "/topology/site-1",
            cookies={"access_token": token},
        )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_no_auth_returns_401(self, client: AsyncClient) -> None:
        resp = await client.get("/topology/site-1")
        assert resp.status_code == 401


class TestUpsertEdgeAPI:
    @pytest.mark.asyncio
    async def test_create_edge(
        self, client: AsyncClient, make_jwt, fake_pool
    ) -> None:
        fake_pool.set_count(2)  # both cameras exist
        fake_pool.set_fetchrow({
            "edge_id": uuid.uuid4(),
            "camera_a_id": "cam-1",
            "camera_b_id": "cam-2",
            "transition_time_s": 10.0,
            "confidence": 0.9,
            "enabled": True,
        })
        token = make_jwt(role="admin")
        resp = await client.put(
            "/topology/site-1/edges",
            json={
                "camera_a_id": "cam-1",
                "camera_b_id": "cam-2",
                "transition_time_s": 10.0,
                "confidence": 0.9,
            },
            cookies={"access_token": token},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["camera_a_id"] == "cam-1"
        assert len(data["transit_distributions"]) == 7

    @pytest.mark.asyncio
    async def test_cameras_not_in_site_returns_404(
        self, client: AsyncClient, make_jwt, fake_pool
    ) -> None:
        fake_pool.set_count(1)  # only 1 camera found
        token = make_jwt(role="admin")
        resp = await client.put(
            "/topology/site-1/edges",
            json={
                "camera_a_id": "cam-1",
                "camera_b_id": "cam-missing",
                "transition_time_s": 10.0,
            },
            cookies={"access_token": token},
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_operator_forbidden(
        self, client: AsyncClient, make_jwt
    ) -> None:
        token = make_jwt(role="operator")
        resp = await client.put(
            "/topology/site-1/edges",
            json={
                "camera_a_id": "cam-1",
                "camera_b_id": "cam-2",
                "transition_time_s": 10.0,
            },
            cookies={"access_token": token},
        )
        assert resp.status_code == 403


class TestAddCameraAPI:
    @pytest.mark.asyncio
    async def test_add_camera(
        self, client: AsyncClient, make_jwt, fake_pool
    ) -> None:
        fake_pool.set_count(1)  # site exists
        token = make_jwt(role="admin")
        resp = await client.post(
            "/topology/site-1/cameras",
            json={
                "camera_id": "cam-new",
                "name": "New Camera",
                "zone_id": "lobby",
                "latitude": 40.71,
            },
            cookies={"access_token": token},
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["camera_id"] == "cam-new"
        assert data["zone_id"] == "lobby"

    @pytest.mark.asyncio
    async def test_site_not_found(
        self, client: AsyncClient, make_jwt, fake_pool
    ) -> None:
        fake_pool.set_count(0)  # site not found
        token = make_jwt(role="admin")
        resp = await client.post(
            "/topology/site-1/cameras",
            json={"camera_id": "c", "name": "n"},
            cookies={"access_token": token},
        )
        assert resp.status_code == 404


class TestDeleteCameraAPI:
    @pytest.mark.asyncio
    async def test_delete_camera(
        self, client: AsyncClient, make_jwt, fake_pool
    ) -> None:
        fake_pool.set_execute_result("DELETE 1")
        token = make_jwt(role="admin")
        resp = await client.delete(
            "/topology/site-1/cameras/cam-1",
            cookies={"access_token": token},
        )
        assert resp.status_code == 204

    @pytest.mark.asyncio
    async def test_delete_not_found(
        self, client: AsyncClient, make_jwt, fake_pool
    ) -> None:
        fake_pool.set_execute_result("DELETE 0")
        token = make_jwt(role="admin")
        resp = await client.delete(
            "/topology/site-1/cameras/cam-missing",
            cookies={"access_token": token},
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_operator_forbidden(
        self, client: AsyncClient, make_jwt
    ) -> None:
        token = make_jwt(role="operator")
        resp = await client.delete(
            "/topology/site-1/cameras/cam-1",
            cookies={"access_token": token},
        )
        assert resp.status_code == 403
