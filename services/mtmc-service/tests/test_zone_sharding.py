"""Tests for zone-based MTMC sharding.

Creates synthetic multi-zone topologies and verifies zone membership,
boundary detection, embedding filtering, boundary event publishing,
and dynamic topology refresh.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock
from uuid import uuid4

import numpy as np

from helpers import make_l2_normalised
from topology_client import CameraNode, TopologyGraph, TransitionEdge
from zone_sharding import ZoneBoundaryEvent, ZoneShardingManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_topology(
    cameras: list[CameraNode],
    edges: list[TransitionEdge],
    site_id: str = "test-site",
) -> TopologyGraph:
    return TopologyGraph(site_id=site_id, cameras=cameras, edges=edges)


def _make_topo_client(graph: TopologyGraph | None) -> MagicMock:
    """Mock TopologyClient with a pre-built graph."""
    topo = MagicMock()
    topo.graph = graph
    return topo


def _three_zone_topology() -> TopologyGraph:
    """Three zones (entrance, lobby, parking) with cross-zone boundary edges.

    Layout::

        entrance:  cam-e1, cam-e2
        lobby:     cam-l1, cam-l2
        parking:   cam-p1, cam-p2

    Edges:
        cam-e2 <-> cam-l1   (entrance <-> lobby boundary)
        cam-l2 <-> cam-p1   (lobby <-> parking boundary)
        cam-e1 <-> cam-e2   (intra-zone entrance)
        cam-l1 <-> cam-l2   (intra-zone lobby)
        cam-p1 <-> cam-p2   (intra-zone parking)
    """
    cameras = [
        CameraNode(camera_id="cam-e1", site_id="test-site", name="Entrance 1", zone_id="entrance"),
        CameraNode(camera_id="cam-e2", site_id="test-site", name="Entrance 2", zone_id="entrance"),
        CameraNode(camera_id="cam-l1", site_id="test-site", name="Lobby 1", zone_id="lobby"),
        CameraNode(camera_id="cam-l2", site_id="test-site", name="Lobby 2", zone_id="lobby"),
        CameraNode(camera_id="cam-p1", site_id="test-site", name="Parking 1", zone_id="parking"),
        CameraNode(camera_id="cam-p2", site_id="test-site", name="Parking 2", zone_id="parking"),
    ]
    edges = [
        # Cross-zone
        TransitionEdge(camera_a_id="cam-e2", camera_b_id="cam-l1", transition_time_s=10.0),
        TransitionEdge(camera_a_id="cam-l2", camera_b_id="cam-p1", transition_time_s=15.0),
        # Intra-zone
        TransitionEdge(camera_a_id="cam-e1", camera_b_id="cam-e2", transition_time_s=5.0),
        TransitionEdge(camera_a_id="cam-l1", camera_b_id="cam-l2", transition_time_s=8.0),
        TransitionEdge(camera_a_id="cam-p1", camera_b_id="cam-p2", transition_time_s=12.0),
    ]
    return _make_topology(cameras, edges)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_zone_cameras_identified() -> None:
    """3 zones — verify each zone's camera set."""
    graph = _three_zone_topology()

    for zone_id, expected in [
        ("entrance", {"cam-e1", "cam-e2"}),
        ("lobby", {"cam-l1", "cam-l2"}),
        ("parking", {"cam-p1", "cam-p2"}),
    ]:
        topo = _make_topo_client(graph)
        mgr = ZoneShardingManager(topo, zone_id=zone_id)
        assert mgr.get_zone_cameras() == expected, f"Zone {zone_id} cameras mismatch"


def test_boundary_cameras_detected() -> None:
    """Cameras with cross-zone edges are identified as boundary cameras."""
    graph = _three_zone_topology()

    # Entrance zone: cam-e2 has edge to lobby
    topo = _make_topo_client(graph)
    mgr = ZoneShardingManager(topo, zone_id="entrance")
    assert mgr.get_boundary_cameras() == {"cam-e2"}
    assert mgr.is_boundary_camera("cam-e2")
    assert not mgr.is_boundary_camera("cam-e1")
    assert mgr.get_adjacent_zones("cam-e2") == {"lobby"}

    # Lobby zone: cam-l1 borders entrance, cam-l2 borders parking
    topo2 = _make_topo_client(graph)
    mgr2 = ZoneShardingManager(topo2, zone_id="lobby")
    assert mgr2.get_boundary_cameras() == {"cam-l1", "cam-l2"}
    assert mgr2.get_adjacent_zones("cam-l1") == {"entrance"}
    assert mgr2.get_adjacent_zones("cam-l2") == {"parking"}

    # Parking zone: cam-p1 borders lobby
    topo3 = _make_topo_client(graph)
    mgr3 = ZoneShardingManager(topo3, zone_id="parking")
    assert mgr3.get_boundary_cameras() == {"cam-p1"}
    assert mgr3.get_adjacent_zones("cam-p1") == {"lobby"}


def test_no_zone_config_passes_all() -> None:
    """zone_id=None accepts all cameras (backward-compatible)."""
    graph = _three_zone_topology()
    topo = _make_topo_client(graph)
    mgr = ZoneShardingManager(topo, zone_id=None)

    # All 6 cameras should be in scope
    all_cams = {"cam-e1", "cam-e2", "cam-l1", "cam-l2", "cam-p1", "cam-p2"}
    assert mgr.get_zone_cameras() == all_cams

    # is_in_zone returns True for every camera
    for cam_id in all_cams:
        assert mgr.is_in_zone(cam_id)

    # No boundary cameras when unsharded
    assert mgr.get_boundary_cameras() == set()
    for cam_id in all_cams:
        assert not mgr.is_boundary_camera(cam_id)


def test_embedding_filtered_by_zone() -> None:
    """Embedding from a camera outside the zone is filtered out."""
    graph = _three_zone_topology()
    topo = _make_topo_client(graph)
    mgr = ZoneShardingManager(topo, zone_id="entrance")

    # Entrance cameras pass
    assert mgr.is_in_zone("cam-e1")
    assert mgr.is_in_zone("cam-e2")

    # Lobby and parking cameras are filtered
    assert not mgr.is_in_zone("cam-l1")
    assert not mgr.is_in_zone("cam-l2")
    assert not mgr.is_in_zone("cam-p1")
    assert not mgr.is_in_zone("cam-p2")

    # Unknown camera is also filtered
    assert not mgr.is_in_zone("cam-unknown")


def test_boundary_event_published(rng: np.random.Generator) -> None:
    """Track closure at a boundary camera triggers a cross-zone publish."""
    graph = _three_zone_topology()
    topo = _make_topo_client(graph)
    mgr = ZoneShardingManager(topo, zone_id="entrance")

    # cam-e2 is a boundary camera
    assert mgr.is_boundary_camera("cam-e2")

    # Create a boundary event
    vec = make_l2_normalised(rng=rng)
    event = ZoneBoundaryEvent(
        local_track_id=str(uuid4()),
        camera_id="cam-e2",
        zone_id="entrance",
        embedding_vector=vec.tolist(),
        model_version="1.0.0",
        object_class="person",
        timestamp=time.time(),
    )

    # Mock Kafka producer
    producer = MagicMock()
    mgr.publish_boundary_event(producer, "mtmc.cross_zone", event)

    producer.produce.assert_called_once()
    call_kwargs = producer.produce.call_args
    assert call_kwargs.kwargs["topic"] == "mtmc.cross_zone"
    assert call_kwargs.kwargs["key"] == event.local_track_id.encode("utf-8")
    producer.flush.assert_called_once()

    # Verify round-trip serialization
    raw = call_kwargs.kwargs["value"]
    restored = ZoneBoundaryEvent.from_bytes(raw)
    assert restored.local_track_id == event.local_track_id
    assert restored.camera_id == "cam-e2"
    assert restored.zone_id == "entrance"
    assert restored.object_class == "person"
    assert len(restored.embedding_vector) == 512


def test_zone_refresh() -> None:
    """Topology change updates zone membership."""
    # Start with entrance zone
    graph1 = _three_zone_topology()
    topo = _make_topo_client(graph1)
    mgr = ZoneShardingManager(topo, zone_id="entrance")

    assert mgr.get_zone_cameras() == {"cam-e1", "cam-e2"}

    # Simulate topology change: add a new camera to entrance zone
    new_cameras = list(graph1.cameras) + [
        CameraNode(
            camera_id="cam-e3",
            site_id="test-site",
            name="Entrance 3",
            zone_id="entrance",
        ),
    ]
    new_edges = list(graph1.edges) + [
        TransitionEdge(
            camera_a_id="cam-e2",
            camera_b_id="cam-e3",
            transition_time_s=4.0,
        ),
    ]
    graph2 = _make_topology(new_cameras, new_edges)
    topo.graph = graph2

    mgr.refresh()

    assert mgr.get_zone_cameras() == {"cam-e1", "cam-e2", "cam-e3"}
    # cam-e2 is still boundary (edge to lobby)
    assert mgr.is_boundary_camera("cam-e2")
    # cam-e3 is NOT boundary (only intra-zone edge)
    assert not mgr.is_boundary_camera("cam-e3")
