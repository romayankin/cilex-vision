"""Topology graph client — loads camera topology from DB, caches, and
provides candidate camera filtering for MTMC matching.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

import asyncpg

logger = logging.getLogger(__name__)

# Import topology models from the topology service
# At runtime, the topology service is a sibling package; we import its models
# from a vendored copy or via sys.path.  The models are pure Pydantic with no
# heavy dependencies, so they can be imported standalone.
try:
    from topology_models import (
        CameraNode,
        TopologyGraph,
        TransitionEdge,
    )
except ImportError:
    # Fallback: inline minimal models matching the topology service
    from pydantic import BaseModel, Field

    class TransitTimeDistribution(BaseModel):  # type: ignore[no-redef]
        object_class: str
        p50_ms: float
        p90_ms: float
        p99_ms: float
        sample_count: int = 0

    class CameraNode(BaseModel):  # type: ignore[no-redef]
        camera_id: str
        site_id: str
        name: str
        zone_id: Optional[str] = None
        status: str = "offline"

    class TransitionEdge(BaseModel):  # type: ignore[no-redef]
        edge_id: Optional[str] = None
        camera_a_id: str
        camera_b_id: str
        transition_time_s: float
        confidence: float = 1.0
        enabled: bool = True
        transit_distributions: list[TransitTimeDistribution] = Field(default_factory=list)  # type: ignore[assignment]

        @staticmethod
        def default_distributions(transition_time_s: float) -> list[TransitTimeDistribution]:  # type: ignore[override]
            OBJECT_CLASSES = ["person", "car", "truck", "bus", "bicycle", "motorcycle", "animal"]
            SPEED = {"person": 1.0, "car": 0.3, "truck": 0.5, "bus": 0.4, "bicycle": 0.6, "motorcycle": 0.35, "animal": 0.8}
            dists = []
            for cls in OBJECT_CLASSES:
                base_ms = transition_time_s * 1000.0 * SPEED.get(cls, 1.0)
                dists.append(TransitTimeDistribution(
                    object_class=cls, p50_ms=round(base_ms, 1),
                    p90_ms=round(base_ms * 1.5, 1), p99_ms=round(base_ms * 2.5, 1),
                ))
            return dists

    class TopologyGraph(BaseModel):  # type: ignore[no-redef]
        site_id: str
        cameras: list[CameraNode] = Field(default_factory=list)  # type: ignore[assignment]
        edges: list[TransitionEdge] = Field(default_factory=list)  # type: ignore[assignment]

        def downstream_cameras(self, camera_id: str, time_window_ms: float, object_class: str = "person") -> list[str]:
            SPEED = {"person": 1.0, "car": 0.3, "truck": 0.5, "bus": 0.4, "bicycle": 0.6, "motorcycle": 0.35, "animal": 0.8}
            adj: dict[str, list[tuple[str, float]]] = {}
            for edge in self.edges:
                if not edge.enabled:
                    continue
                cost = None
                for d in edge.transit_distributions:
                    if d.object_class == object_class:
                        cost = d.p99_ms
                        break
                if cost is None:
                    cost = edge.transition_time_s * 1000.0 * SPEED.get(object_class, 1.0) * 2.5
                adj.setdefault(edge.camera_a_id, []).append((edge.camera_b_id, cost))
                adj.setdefault(edge.camera_b_id, []).append((edge.camera_a_id, cost))
            visited: dict[str, float] = {camera_id: 0.0}
            queue = [(camera_id, 0.0)]
            while queue:
                current, cc = queue.pop(0)
                for nb, ec in adj.get(current, []):
                    total = cc + ec
                    if total <= time_window_ms and (nb not in visited or total < visited[nb]):
                        visited[nb] = total
                        queue.append((nb, total))
            visited.pop(camera_id, None)
            return sorted(visited.keys())


class TopologyClient:
    """Loads topology graph from DB and provides candidate filtering."""

    def __init__(self, pool: asyncpg.Pool, site_id: str, refresh_interval_s: int = 300) -> None:
        self._pool = pool
        self._site_id = site_id
        self._refresh_interval_s = refresh_interval_s
        self._graph: Optional[TopologyGraph] = None
        self._last_refresh = 0.0
        # Cache: (camera_id, object_class) -> (set of camera_ids, expiry)
        self._candidate_cache: dict[tuple[str, str], tuple[set[str], float]] = {}

    @property
    def graph(self) -> Optional[TopologyGraph]:
        return self._graph

    async def load(self) -> TopologyGraph:
        """Load topology graph from the database."""
        async with self._pool.acquire() as conn:
            cam_rows = await conn.fetch(
                "SELECT camera_id, site_id, name, config_json, status "
                "FROM cameras WHERE site_id = $1",
                self._site_id,
            )
            edge_rows = await conn.fetch(
                "SELECT e.edge_id, e.camera_a_id, e.camera_b_id, "
                "       e.transition_time_s, e.confidence, e.enabled "
                "FROM topology_edges e "
                "JOIN cameras ca ON e.camera_a_id = ca.camera_id "
                "WHERE ca.site_id = $1",
                self._site_id,
            )

        cameras: list[Any] = []
        for row in cam_rows:
            zone_id = None
            cfg = row["config_json"]
            if isinstance(cfg, dict):
                zone_id = cfg.get("zone_id")
            cameras.append(CameraNode(
                camera_id=row["camera_id"],
                site_id=str(row["site_id"]),
                name=row["name"],
                zone_id=zone_id,
                status=row["status"],
            ))

        edges: list[Any] = []
        for row in edge_rows:
            edge = TransitionEdge(
                edge_id=str(row["edge_id"]),
                camera_a_id=row["camera_a_id"],
                camera_b_id=row["camera_b_id"],
                transition_time_s=row["transition_time_s"],
                confidence=row["confidence"],
                enabled=row["enabled"],
            )
            edge.transit_distributions = TransitionEdge.default_distributions(
                row["transition_time_s"]
            )
            edges.append(edge)

        self._graph = TopologyGraph(
            site_id=self._site_id,
            cameras=cameras,
            edges=edges,
        )
        self._last_refresh = time.time()
        self._candidate_cache.clear()
        logger.info(
            "Loaded topology: %d cameras, %d edges",
            len(cameras),
            len(edges),
        )
        return self._graph

    async def maybe_refresh(self) -> None:
        """Refresh the graph if the refresh interval has elapsed."""
        if (time.time() - self._last_refresh) >= self._refresh_interval_s:
            await self.load()

    def get_candidates(
        self,
        camera_id: str,
        object_class: str,
        active_horizon_ms: float,
    ) -> set[str]:
        """Return camera IDs reachable from *camera_id* within the time window."""
        if self._graph is None:
            return set()

        key = (camera_id, object_class)
        cached = self._candidate_cache.get(key)
        if cached is not None and cached[1] > time.time():
            return cached[0]

        candidates = set(
            self._graph.downstream_cameras(camera_id, active_horizon_ms, object_class)
        )
        # Cache for 30 seconds — topology doesn't change that fast
        self._candidate_cache[key] = (candidates, time.time() + 30.0)
        return candidates

    def get_edge_distribution(
        self,
        from_camera: str,
        to_camera: str,
        object_class: str,
    ) -> Optional[tuple[float, float, float]]:
        """Return (p50_ms, p90_ms, p99_ms) for the edge between two cameras.

        Returns None if no edge exists.
        """
        if self._graph is None:
            return None

        for edge in self._graph.edges:
            if not edge.enabled:
                continue
            if not (
                (edge.camera_a_id == from_camera and edge.camera_b_id == to_camera)
                or (edge.camera_a_id == to_camera and edge.camera_b_id == from_camera)
            ):
                continue
            for dist in edge.transit_distributions:
                if dist.object_class == object_class:
                    return (dist.p50_ms, dist.p90_ms, dist.p99_ms)
            # Fallback: derive from transition_time_s
            speed = {"person": 1.0, "car": 0.3, "truck": 0.5, "bus": 0.4,
                     "bicycle": 0.6, "motorcycle": 0.35, "animal": 0.8}
            base = edge.transition_time_s * 1000.0 * speed.get(object_class, 1.0)
            return (round(base, 1), round(base * 1.5, 1), round(base * 2.5, 1))

        return None
