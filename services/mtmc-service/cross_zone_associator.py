"""Cross-zone track associator.

Lighter-weight matching service that runs at lower frequency, consuming
zone-boundary track closures and attempting to link zone_global_tracks
across adjacent zones.

Data flow:
  zone_global_track (zone A) + zone_global_track (zone B) -> site_global_link
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import faiss
import numpy as np
from prometheus_client import Counter, Histogram, start_http_server

from config import MTMCSettings
from zone_sharding import ZoneBoundaryEvent

logger = logging.getLogger(__name__)


# -- Prometheus metrics -------------------------------------------------------

CROSS_ZONE_MATCHES = Counter(
    "cross_zone_matches_total",
    "Total successful cross-zone track matches",
)

CROSS_ZONE_CANDIDATES = Counter(
    "cross_zone_candidates_total",
    "Total cross-zone match candidates evaluated",
)

CROSS_ZONE_LATENCY = Histogram(
    "cross_zone_latency_ms",
    "Latency of cross-zone batch matching in milliseconds",
    buckets=[1, 5, 10, 25, 50, 100, 250, 500, 1000],
)


# -- Data classes -------------------------------------------------------------


@dataclass
class CrossZoneMatch:
    """A confirmed cross-zone track association."""

    zone_a_track_id: str
    zone_a_zone_id: str
    zone_b_track_id: str
    zone_b_zone_id: str
    cosine_score: float
    object_class: str
    model_version: str


# -- Boundary FAISS index ----------------------------------------------------


class BoundaryIndex:
    """Small FAISS index for boundary track embeddings from one zone.

    Thread-safe wrapper around ``faiss.IndexFlatIP`` for cross-zone matching.
    """

    def __init__(self, dimension: int = 512) -> None:
        self._dimension = dimension
        self._lock = threading.Lock()
        flat = faiss.IndexFlatIP(dimension)
        self._index: faiss.IndexIDMap = faiss.IndexIDMap(flat)
        self._metadata: dict[int, ZoneBoundaryEvent] = {}
        self._track_map: dict[str, int] = {}
        self._next_id: int = 0

    def add(self, event: ZoneBoundaryEvent) -> int:
        """Add a boundary event embedding to the index."""
        vec = np.array(event.embedding_vector, dtype=np.float32)
        vec = np.ascontiguousarray(vec.reshape(1, -1))

        with self._lock:
            old_id = self._track_map.get(event.local_track_id)
            if old_id is not None:
                self._remove_by_id(old_id)

            fid = self._next_id
            self._next_id += 1

            ids = np.array([fid], dtype=np.int64)
            self._index.add_with_ids(vec, ids)
            self._metadata[fid] = event
            self._track_map[event.local_track_id] = fid

        return fid

    def search(
        self, vector: list[float], k: int = 10
    ) -> list[tuple[ZoneBoundaryEvent, float]]:
        """Search for nearest boundary embeddings."""
        vec = np.array(vector, dtype=np.float32)
        vec = np.ascontiguousarray(vec.reshape(1, -1))

        with self._lock:
            n: int = self._index.ntotal
            if n == 0:
                return []
            effective_k = min(k, n)
            scores, ids = self._index.search(vec, effective_k)

        results: list[tuple[ZoneBoundaryEvent, float]] = []
        for score, fid in zip(scores[0], ids[0]):
            if fid == -1:
                continue
            meta = self._metadata.get(int(fid))
            if meta is None:
                continue
            results.append((meta, float(score)))
        return results

    def size(self) -> int:
        """Return the number of embeddings in this index."""
        with self._lock:
            return int(self._index.ntotal)

    def _remove_by_id(self, fid: int) -> None:
        """Remove by FAISS ID. Caller must hold the lock."""
        meta = self._metadata.pop(fid, None)
        if meta is not None:
            self._track_map.pop(meta.local_track_id, None)
        ids_arr = np.array([fid], dtype=np.int64)
        self._index.remove_ids(ids_arr)


# -- Cross-zone associator ---------------------------------------------------


class CrossZoneAssociator:
    """Cross-zone track matcher.

    Consumes zone boundary events and matches tracks across adjacent zones
    using a relaxed threshold.  Runs at lower frequency (batched every N
    seconds) rather than per-message.
    """

    def __init__(
        self,
        settings: MTMCSettings,
        zone_adjacency: dict[str, set[str]] | None = None,
        db_writer: Any = None,
    ) -> None:
        self._settings = settings
        self._match_threshold = settings.cross_zone_match_threshold
        self._batch_interval = settings.cross_zone_batch_interval_s

        # Per-zone boundary indices: zone_id -> BoundaryIndex
        self._zone_indices: dict[str, BoundaryIndex] = {}

        # Zone adjacency: zone_id -> set of adjacent zone_ids
        self._zone_adjacency: dict[str, set[str]] = zone_adjacency or {}

        # Pending events buffer for batch processing
        self._pending_events: list[ZoneBoundaryEvent] = []
        self._lock = threading.Lock()

        self._db: Any = db_writer
        self._shutdown = asyncio.Event()

    def set_zone_adjacency(self, adjacency: dict[str, set[str]]) -> None:
        """Update the zone adjacency map."""
        self._zone_adjacency = adjacency

    def _get_or_create_index(self, zone_id: str) -> BoundaryIndex:
        """Get or lazily create a boundary index for a zone."""
        if zone_id not in self._zone_indices:
            self._zone_indices[zone_id] = BoundaryIndex(dimension=512)
        return self._zone_indices[zone_id]

    def ingest_event(self, event: ZoneBoundaryEvent) -> None:
        """Add a boundary event to the zone index and pending buffer."""
        index = self._get_or_create_index(event.zone_id)
        index.add(event)

        with self._lock:
            self._pending_events.append(event)

    def match_batch(self) -> list[CrossZoneMatch]:
        """Process pending boundary events and attempt cross-zone matching.

        For each pending event, search adjacent zone indices for candidates
        that pass class, version, and threshold checks.
        """
        with self._lock:
            events = list(self._pending_events)
            self._pending_events.clear()

        if not events:
            return []

        start = time.monotonic()
        matches: list[CrossZoneMatch] = []

        for event in events:
            adjacent = self._zone_adjacency.get(event.zone_id, set())
            if not adjacent:
                continue

            for adj_zone in adjacent:
                adj_index = self._zone_indices.get(adj_zone)
                if adj_index is None or adj_index.size() == 0:
                    continue

                results = adj_index.search(event.embedding_vector, k=5)

                for candidate, score in results:
                    CROSS_ZONE_CANDIDATES.inc()

                    # Class consistency
                    if candidate.object_class != event.object_class:
                        continue
                    # Model version boundary (ADR-008)
                    if candidate.model_version != event.model_version:
                        continue
                    # Skip self
                    if candidate.local_track_id == event.local_track_id:
                        continue
                    # Threshold
                    if score < self._match_threshold:
                        continue

                    matches.append(
                        CrossZoneMatch(
                            zone_a_track_id=event.local_track_id,
                            zone_a_zone_id=event.zone_id,
                            zone_b_track_id=candidate.local_track_id,
                            zone_b_zone_id=candidate.zone_id,
                            cosine_score=score,
                            object_class=event.object_class,
                            model_version=event.model_version,
                        )
                    )
                    CROSS_ZONE_MATCHES.inc()
                    logger.info(
                        "Cross-zone match: %s@%s <-> %s@%s (score=%.3f)",
                        event.local_track_id,
                        event.zone_id,
                        candidate.local_track_id,
                        candidate.zone_id,
                        score,
                    )
                    break  # Take best match per event per adjacent zone

        elapsed_ms = (time.monotonic() - start) * 1000
        CROSS_ZONE_LATENCY.observe(elapsed_ms)

        return matches

    async def persist_matches(self, matches: list[CrossZoneMatch]) -> None:
        """Persist cross-zone matches as site_global_link records.

        Requires a *db_writer* with a ``create_site_global_link`` method.
        No-op when no db_writer is configured.
        """
        if self._db is None or not matches:
            return
        for m in matches:
            await self._db.create_site_global_link(
                zone_a_track_id=m.zone_a_track_id,
                zone_a_zone_id=m.zone_a_zone_id,
                zone_b_track_id=m.zone_b_track_id,
                zone_b_zone_id=m.zone_b_zone_id,
                confidence=m.cosine_score,
            )

    async def run_consumer(self) -> None:
        """Run the Kafka consumer loop for cross-zone events."""
        from confluent_kafka import Consumer, KafkaError  # noqa: PLC0415

        cfg = self._settings
        consumer_config: dict[str, str | int] = {
            "bootstrap.servers": cfg.kafka_bootstrap,
            "group.id": f"{cfg.kafka_group_id}-cross-zone",
            "auto.offset.reset": "latest",
            "enable.auto.commit": "false",
            "partition.assignment.strategy": "cooperative-sticky",
        }
        if cfg.kafka_security_protocol != "PLAINTEXT":
            consumer_config["security.protocol"] = cfg.kafka_security_protocol

        consumer = Consumer(consumer_config)
        consumer.subscribe([cfg.cross_zone_topic])

        logger.info(
            "Cross-zone associator consuming from %s", cfg.cross_zone_topic
        )

        last_batch = time.monotonic()

        try:
            while not self._shutdown.is_set():
                msg = await asyncio.to_thread(
                    consumer.poll,
                    min(cfg.kafka_poll_timeout_s, self._batch_interval),
                )

                if msg is not None and not msg.error():
                    if msg.value() is not None:
                        try:
                            event = ZoneBoundaryEvent.from_bytes(msg.value())
                            self.ingest_event(event)
                        except Exception:
                            logger.warning(
                                "Failed to parse boundary event",
                                exc_info=True,
                            )
                    await asyncio.to_thread(
                        consumer.commit, asynchronous=False
                    )
                elif msg is not None and msg.error():
                    err = msg.error()
                    if err.code() != KafkaError._PARTITION_EOF:
                        logger.error("Kafka error: %s", err)

                # Batch matching at interval
                now = time.monotonic()
                if (now - last_batch) >= self._batch_interval:
                    matches = self.match_batch()
                    if matches:
                        await self.persist_matches(matches)
                    last_batch = now

        finally:
            consumer.close()

    async def shutdown(self) -> None:
        """Signal shutdown and run a final batch."""
        self._shutdown.set()
        matches = self.match_batch()
        if matches:
            await self.persist_matches(matches)


# -- CLI entry point ----------------------------------------------------------

DEFAULT_CONFIG = Path(__file__).resolve().parent / "config.yaml"


async def _run(settings: MTMCSettings) -> None:
    start_http_server(settings.metrics_port + 1)
    associator = CrossZoneAssociator(settings)
    try:
        await associator.run_consumer()
    except asyncio.CancelledError:
        pass
    finally:
        await associator.shutdown()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Cross-zone track associator for MTMC"
    )
    parser.add_argument(
        "--config", type=Path, default=DEFAULT_CONFIG, help="YAML config path"
    )
    parser.add_argument(
        "--zone-id", type=str, default=None, help="Override zone_id"
    )
    args = parser.parse_args()

    settings = MTMCSettings.from_yaml(args.config)
    if args.zone_id is not None:
        settings = settings.model_copy(update={"zone_id": args.zone_id})

    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    logger.info("Starting cross-zone associator")
    asyncio.run(_run(settings))


if __name__ == "__main__":
    main()
