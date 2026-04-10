"""Zone-based sharding for large-site MTMC matching.

When zone config is present, the MTMC instance owns one zone and only indexes
embeddings from cameras in that zone. Backward-compatible: if no zone_id is
configured, the instance processes all cameras in the site (existing behavior).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from topology_client import TopologyClient

logger = logging.getLogger(__name__)


@dataclass
class ZoneConfig:
    """Configuration for zone-based sharding."""

    zone_id: str | None = None  # None = no sharding, process full site
    boundary_cameras: set[str] = field(default_factory=set)
    zone_cameras: set[str] = field(default_factory=set)


@dataclass
class ZoneBoundaryEvent:
    """Event published when a track closes at a zone boundary camera.

    Serialized as JSON for the ``mtmc.cross_zone`` Kafka topic.
    A proper Protobuf schema should replace this in a future task.
    """

    local_track_id: str
    camera_id: str
    zone_id: str
    embedding_vector: list[float]
    model_version: str
    object_class: str
    timestamp: float
    global_track_id: str | None = None

    def to_bytes(self) -> bytes:
        """Serialize to JSON bytes for Kafka."""
        return json.dumps(
            {
                "local_track_id": self.local_track_id,
                "camera_id": self.camera_id,
                "zone_id": self.zone_id,
                "embedding_vector": self.embedding_vector,
                "model_version": self.model_version,
                "object_class": self.object_class,
                "timestamp": self.timestamp,
                "global_track_id": self.global_track_id,
            }
        ).encode("utf-8")

    @classmethod
    def from_bytes(cls, data: bytes) -> ZoneBoundaryEvent:
        """Deserialize from JSON bytes."""
        d: dict[str, Any] = json.loads(data.decode("utf-8"))
        return cls(
            local_track_id=d["local_track_id"],
            camera_id=d["camera_id"],
            zone_id=d["zone_id"],
            embedding_vector=d["embedding_vector"],
            model_version=d["model_version"],
            object_class=d["object_class"],
            timestamp=d["timestamp"],
            global_track_id=d.get("global_track_id"),
        )


class ZoneShardingManager:
    """Manages zone-based filtering for MTMC matching.

    When ``zone_id`` is ``None``, all cameras pass through (backward-compatible).
    When set, only cameras in that zone are processed, and boundary cameras
    (with edges to other zones) are identified for cross-zone event publishing.
    """

    def __init__(
        self,
        topology_client: TopologyClient,
        zone_id: str | None = None,
    ) -> None:
        self._topo = topology_client
        self._zone_id = zone_id
        self._zone_cameras: set[str] = set()
        self._boundary_cameras: set[str] = set()
        self._camera_zone_map: dict[str, str | None] = {}
        self._boundary_adjacent_zones: dict[str, set[str]] = {}
        self.refresh()

    @property
    def zone_id(self) -> str | None:
        return self._zone_id

    def is_in_zone(self, camera_id: str) -> bool:
        """Check if a camera belongs to this zone.

        Always returns ``True`` if no zone_id is configured.
        """
        if self._zone_id is None:
            return True
        return camera_id in self._zone_cameras

    def is_boundary_camera(self, camera_id: str) -> bool:
        """Check if a camera is at a zone boundary.

        A boundary camera has enabled edges to cameras in different zones.
        Returns ``False`` if no zone_id is configured.
        """
        if self._zone_id is None:
            return False
        return camera_id in self._boundary_cameras

    def get_zone_cameras(self) -> set[str]:
        """Return all camera IDs in this zone.

        Returns all cameras if no zone_id is configured.
        """
        return set(self._zone_cameras)

    def get_boundary_cameras(self) -> set[str]:
        """Return camera IDs at zone boundaries.

        Returns empty set if no zone_id is configured.
        """
        return set(self._boundary_cameras)

    def get_adjacent_zones(self, camera_id: str) -> set[str]:
        """Return zone IDs reachable from a boundary camera.

        Returns empty set if camera is not a boundary camera.
        """
        return set(self._boundary_adjacent_zones.get(camera_id, set()))

    def refresh(self) -> None:
        """Reload zone membership from the topology graph.

        Called when topology is refreshed.
        """
        graph = self._topo.graph
        if graph is None:
            self._zone_cameras = set()
            self._boundary_cameras = set()
            self._camera_zone_map = {}
            self._boundary_adjacent_zones = {}
            return

        # Build camera -> zone map
        self._camera_zone_map = {}
        for cam in graph.cameras:
            self._camera_zone_map[str(cam.camera_id)] = (
                str(cam.zone_id) if cam.zone_id is not None else None
            )

        if self._zone_id is None:
            # No sharding: all cameras are in scope, no boundaries
            self._zone_cameras = set(self._camera_zone_map.keys())
            self._boundary_cameras = set()
            self._boundary_adjacent_zones = {}
            return

        # Compute cameras belonging to this zone
        self._zone_cameras = {
            cam_id
            for cam_id, zid in self._camera_zone_map.items()
            if zid == self._zone_id
        }

        # Compute boundary cameras: cameras in this zone with edges to other zones
        self._boundary_cameras = set()
        self._boundary_adjacent_zones = {}

        for edge in graph.edges:
            if not edge.enabled:
                continue

            a_id = str(edge.camera_a_id)
            b_id = str(edge.camera_b_id)
            a_zone = self._camera_zone_map.get(a_id)
            b_zone = self._camera_zone_map.get(b_id)

            if (
                a_zone == self._zone_id
                and b_zone is not None
                and b_zone != self._zone_id
            ):
                self._boundary_cameras.add(a_id)
                self._boundary_adjacent_zones.setdefault(a_id, set()).add(b_zone)

            if (
                b_zone == self._zone_id
                and a_zone is not None
                and a_zone != self._zone_id
            ):
                self._boundary_cameras.add(b_id)
                self._boundary_adjacent_zones.setdefault(b_id, set()).add(a_zone)

        logger.info(
            "Zone '%s': %d cameras, %d boundary cameras",
            self._zone_id,
            len(self._zone_cameras),
            len(self._boundary_cameras),
        )

    def publish_boundary_event(
        self,
        producer: Any,
        topic: str,
        event: ZoneBoundaryEvent,
    ) -> None:
        """Publish a zone boundary event to Kafka.

        Args:
            producer: confluent_kafka.Producer instance.
            topic: Kafka topic name (e.g. ``mtmc.cross_zone``).
            event: The boundary event to publish.
        """
        producer.produce(
            topic=topic,
            key=event.local_track_id.encode("utf-8"),
            value=event.to_bytes(),
        )
        producer.flush()
        logger.debug(
            "Published boundary event: track=%s cam=%s zone=%s",
            event.local_track_id,
            event.camera_id,
            event.zone_id,
        )
