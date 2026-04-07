"""Pydantic models for the camera topology graph.

These models wrap the ``cameras`` and ``topology_edges`` DB tables from
``services/db/models.py`` (P0-D04).  They add:

- ``zone_id`` on CameraNode (stored in ``cameras.config_json``)
- Per-class adaptive transit-time distributions on each edge
- Graph helper methods: ``adjacent_cameras``, ``downstream_cameras``

The 7 object classes from taxonomy.md each get independent transit-time
distributions (p50/p90/p99) because different object types traverse
camera-to-camera transitions at different speeds.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field

# 7 object classes from docs/taxonomy.md
OBJECT_CLASSES: list[str] = [
    "person",
    "car",
    "truck",
    "bus",
    "bicycle",
    "motorcycle",
    "animal",
]

# Per-class speed factor relative to person transit time.
# person = 1.0 baseline.  Vehicles are faster, animals slightly slower.
_CLASS_SPEED_FACTOR: dict[str, float] = {
    "person": 1.0,
    "car": 0.3,
    "truck": 0.5,
    "bus": 0.4,
    "bicycle": 0.6,
    "motorcycle": 0.35,
    "animal": 0.8,
}


# ---------------------------------------------------------------------------
# Transit-time distribution
# ---------------------------------------------------------------------------


class TransitTimeDistribution(BaseModel):
    """Transit-time distribution for one object class on one edge.

    ``p50_ms`` / ``p90_ms`` / ``p99_ms`` represent the 50th, 90th and 99th
    percentile transit times in milliseconds.  ``sample_count`` tracks
    how many Re-ID matches have contributed; zero means the distribution
    is still seeded (not yet learned from data).
    """

    object_class: str
    p50_ms: float
    p90_ms: float
    p99_ms: float
    sample_count: int = 0
    last_updated: Optional[datetime] = None


# ---------------------------------------------------------------------------
# Camera node
# ---------------------------------------------------------------------------


class CameraNode(BaseModel):
    """A camera in the topology graph.

    Wraps the ``cameras`` DB table.  ``zone_id`` is stored in the
    ``config_json`` JSONB column and surfaced here for zone-based
    event logic (entered_scene / exited_scene).
    """

    camera_id: str
    site_id: str
    name: str
    zone_id: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    status: str = "offline"
    location_description: Optional[str] = None


# ---------------------------------------------------------------------------
# Transition edge
# ---------------------------------------------------------------------------


class TransitionEdge(BaseModel):
    """A directed edge between two cameras with transit-time distributions.

    ``transition_time_s`` is the overall median person transit time (the
    value stored in the ``topology_edges`` DB table).  ``transit_distributions``
    holds per-class percentile breakdowns.
    """

    edge_id: Optional[str] = None
    camera_a_id: str
    camera_b_id: str
    transition_time_s: float
    confidence: float = 1.0
    enabled: bool = True
    transit_distributions: list[TransitTimeDistribution] = Field(
        default_factory=list
    )

    @staticmethod
    def default_distributions(
        transition_time_s: float,
    ) -> list[TransitTimeDistribution]:
        """Derive per-class seed distributions from a person transit time.

        This produces plausible initial values before the adaptive learning
        service has enough Re-ID matches to refine the percentiles.
        """
        dists: list[TransitTimeDistribution] = []
        for cls in OBJECT_CLASSES:
            factor = _CLASS_SPEED_FACTOR.get(cls, 1.0)
            base_ms = transition_time_s * 1000.0 * factor
            dists.append(
                TransitTimeDistribution(
                    object_class=cls,
                    p50_ms=round(base_ms, 1),
                    p90_ms=round(base_ms * 1.5, 1),
                    p99_ms=round(base_ms * 2.5, 1),
                    sample_count=0,
                )
            )
        return dists


# ---------------------------------------------------------------------------
# Topology graph
# ---------------------------------------------------------------------------


class TopologyGraph(BaseModel):
    """Full topology graph for one site.

    Provides helper methods for MTMC candidate filtering:
    ``adjacent_cameras`` and ``downstream_cameras``.
    """

    site_id: str
    cameras: list[CameraNode] = Field(default_factory=list)
    edges: list[TransitionEdge] = Field(default_factory=list)

    def adjacent_cameras(self, camera_id: str) -> list[str]:
        """Return camera IDs directly connected to *camera_id* via enabled edges."""
        neighbors: set[str] = set()
        for edge in self.edges:
            if not edge.enabled:
                continue
            if edge.camera_a_id == camera_id:
                neighbors.add(edge.camera_b_id)
            elif edge.camera_b_id == camera_id:
                neighbors.add(edge.camera_a_id)
        return sorted(neighbors)

    def downstream_cameras(
        self,
        camera_id: str,
        time_window_ms: float,
        object_class: str = "person",
    ) -> list[str]:
        """Return cameras reachable from *camera_id* within *time_window_ms*.

        Only considers enabled edges whose ``p99_ms`` for *object_class*
        fits within the given time window.  Performs a BFS traversal so
        transitive paths are included.
        """
        # Build adjacency with per-class p99 cost
        adj: dict[str, list[tuple[str, float]]] = {}
        for edge in self.edges:
            if not edge.enabled:
                continue
            cost = self._edge_p99(edge, object_class)
            if cost is None:
                continue
            adj.setdefault(edge.camera_a_id, []).append((edge.camera_b_id, cost))
            adj.setdefault(edge.camera_b_id, []).append((edge.camera_a_id, cost))

        # BFS with cumulative cost
        visited: dict[str, float] = {camera_id: 0.0}
        queue = [(camera_id, 0.0)]
        while queue:
            current, current_cost = queue.pop(0)
            for neighbor, edge_cost in adj.get(current, []):
                total = current_cost + edge_cost
                if total <= time_window_ms and (
                    neighbor not in visited or total < visited[neighbor]
                ):
                    visited[neighbor] = total
                    queue.append((neighbor, total))

        # Remove the source camera itself
        visited.pop(camera_id, None)
        return sorted(visited.keys())

    @staticmethod
    def _edge_p99(edge: TransitionEdge, object_class: str) -> float | None:
        """Get p99 transit time for a class, falling back to transition_time_s."""
        for dist in edge.transit_distributions:
            if dist.object_class == object_class:
                return dist.p99_ms
        # Fallback: derive from transition_time_s
        factor = _CLASS_SPEED_FACTOR.get(object_class, 1.0)
        return edge.transition_time_s * 1000.0 * factor * 2.5


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------


class EdgeCreateRequest(BaseModel):
    """Request body for creating/updating an edge."""

    camera_a_id: str
    camera_b_id: str
    transition_time_s: float
    confidence: float = 1.0
    enabled: bool = True


class CameraCreateRequest(BaseModel):
    """Request body for adding a camera to a site."""

    camera_id: str
    name: str
    zone_id: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    location_description: Optional[str] = None
