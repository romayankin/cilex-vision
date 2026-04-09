"""MTMC Re-ID scoring pipeline.

5-stage matching: topology filter -> FAISS search -> version/class filter ->
transit-time likelihood -> combined scoring with optional colour check.

Assignment: greedy (best match) for < 10 candidates, Hungarian
(scipy.optimize.linear_sum_assignment) for >= 10.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

import numpy as np
from scipy.optimize import linear_sum_assignment

from faiss_index import EmbeddingMeta, FAISSIndex, SearchResult
from metrics import MATCH_SCORE, MATCHES_TOTAL, REJECTS_TOTAL
from publisher import DBWriter
from topology_client import TopologyClient

logger = logging.getLogger(__name__)


@dataclass
class MatchResult:
    """A confirmed cross-camera match."""

    query_local_track_id: str
    matched_local_track_id: str
    matched_camera_id: str
    global_track_id: Optional[UUID]
    combined_score: float
    cosine_score: float
    transit_score: float
    attribute_score: float


class Matcher:
    """Orchestrates the 5-stage MTMC scoring pipeline."""

    def __init__(
        self,
        faiss_index: FAISSIndex,
        topology_client: TopologyClient,
        db_writer: DBWriter,
        site_id: str,
        faiss_k: int = 20,
        match_threshold: float = 0.65,
        active_horizon_minutes: int = 30,
        weight_cosine: float = 0.6,
        weight_transit: float = 0.3,
        weight_attribute: float = 0.1,
    ) -> None:
        self._index = faiss_index
        self._topo = topology_client
        self._db = db_writer
        self._site_id = site_id
        self._k = faiss_k
        self._threshold = match_threshold
        self._horizon_ms = active_horizon_minutes * 60 * 1000.0
        self._w_cos = weight_cosine
        self._w_transit = weight_transit
        self._w_attr = weight_attribute

        # Cache: local_track_id -> (camera_id, object_class)
        self._track_info_cache: dict[str, tuple[str, str]] = {}
        # Cache: local_track_id -> list[color_value]
        self._color_cache: dict[str, list[str]] = {}

    async def process_embedding(
        self,
        embedding_id: str,
        local_track_id: str,
        vector: np.ndarray,
        model_version: str,
        quality_score: float,
        timestamp: float,
    ) -> Optional[MatchResult]:
        """Process a single embedding through the scoring pipeline.

        Returns a MatchResult if a cross-camera match is found, else None.
        """
        # Resolve camera_id and object_class for this track
        track_info = await self._get_track_info(local_track_id)
        if track_info is None:
            REJECTS_TOTAL.labels(site_id=self._site_id, reason="track_not_found").inc()
            logger.debug("Track info not found for %s", local_track_id)
            return None

        camera_id, object_class = track_info

        # Build metadata and add to FAISS
        meta = EmbeddingMeta(
            embedding_id=embedding_id,
            camera_id=camera_id,
            local_track_id=local_track_id,
            object_class=object_class,
            model_version=model_version,
            timestamp=timestamp,
        )
        self._index.add(embedding_id, vector, meta)

        # Stage 1: topology filter — get reachable cameras
        candidate_cameras = self._topo.get_candidates(
            camera_id, object_class, self._horizon_ms
        )
        if not candidate_cameras:
            return None

        # Stage 2: FAISS search — top-K nearest neighbours
        results = self._index.search(vector, self._k)

        # Stage 3 + 4: filter by version boundary, class, topology, self
        filtered = self._filter_candidates(
            results, embedding_id, local_track_id, camera_id,
            object_class, model_version, candidate_cameras,
        )

        if not filtered:
            return None

        # Stage 5 + 6 + 7: score remaining candidates
        scored = await self._score_candidates(
            filtered, camera_id, object_class, local_track_id, timestamp,
        )

        if not scored:
            return None

        # Stage 8: assignment
        match = self._assign(scored)
        if match is None:
            return None

        # Stage 9: create/update global track
        result = await self._persist_match(
            local_track_id, camera_id, object_class, match, timestamp,
        )

        MATCHES_TOTAL.labels(site_id=self._site_id).inc()
        MATCH_SCORE.observe(result.combined_score)
        return result

    def _filter_candidates(
        self,
        results: list[SearchResult],
        query_embedding_id: str,
        query_local_track_id: str,
        query_camera_id: str,
        query_object_class: str,
        query_model_version: str,
        candidate_cameras: set[str],
    ) -> list[SearchResult]:
        """Apply version boundary, class consistency, and topology filters."""
        filtered: list[SearchResult] = []

        for r in results:
            m = r.meta
            # Skip self
            if m.embedding_id == query_embedding_id:
                continue
            # Skip same camera (Re-ID is cross-camera)
            if m.camera_id == query_camera_id:
                continue
            # Skip same local track
            if m.local_track_id == query_local_track_id:
                continue

            # Stage 3: version boundary — CRITICAL
            if m.model_version != query_model_version:
                REJECTS_TOTAL.labels(
                    site_id=self._site_id, reason="version_mismatch"
                ).inc()
                continue

            # Stage 4: class consistency
            if m.object_class != query_object_class:
                REJECTS_TOTAL.labels(
                    site_id=self._site_id, reason="class_mismatch"
                ).inc()
                continue

            # Topology filter: camera must be reachable
            if m.camera_id not in candidate_cameras:
                REJECTS_TOTAL.labels(
                    site_id=self._site_id, reason="topology_unreachable"
                ).inc()
                continue

            filtered.append(r)

        return filtered

    async def _score_candidates(
        self,
        candidates: list[SearchResult],
        query_camera_id: str,
        query_object_class: str,
        query_local_track_id: str,
        query_timestamp: float,
    ) -> list[tuple[SearchResult, float]]:
        """Compute combined score for each candidate."""
        query_colors = await self._get_colors(query_local_track_id)
        scored: list[tuple[SearchResult, float]] = []

        for r in candidates:
            cosine = r.score  # Inner product on L2-normed = cosine similarity

            # Stage 6: transit-time likelihood
            transit = self._transit_likelihood(
                query_camera_id, r.meta.camera_id,
                query_object_class, query_timestamp, r.meta.timestamp,
            )

            # Stage 5: colour attribute check
            attr_bonus = await self._attribute_score(
                query_colors, r.meta.local_track_id,
            )

            combined = (
                self._w_cos * cosine
                + self._w_transit * transit
                + self._w_attr * attr_bonus
            )

            if combined >= self._threshold:
                scored.append((r, combined))
            else:
                REJECTS_TOTAL.labels(
                    site_id=self._site_id, reason="below_threshold"
                ).inc()

        return scored

    def _transit_likelihood(
        self,
        from_camera: str,
        to_camera: str,
        object_class: str,
        from_ts: float,
        to_ts: float,
    ) -> float:
        """Score how likely the observed transit time is.

        Gaussian approximation around p50, penalised beyond p99.
        Returns 0.0 to 1.0.
        """
        dist = self._topo.get_edge_distribution(from_camera, to_camera, object_class)
        if dist is None:
            return 0.5  # Unknown topology — neutral score

        p50_ms, p90_ms, p99_ms = dist
        observed_ms = abs(from_ts - to_ts) * 1000.0

        if observed_ms > p99_ms:
            # Beyond p99 — sharp penalty
            overshoot = (observed_ms - p99_ms) / max(p99_ms, 1.0)
            return max(0.0, 0.2 * math.exp(-overshoot))

        # Gaussian around p50 with sigma derived from p90
        # p90 ~= p50 + 1.28 * sigma
        sigma = max((p90_ms - p50_ms) / 1.28, 1.0)
        z = (observed_ms - p50_ms) / sigma
        return math.exp(-0.5 * z * z)

    async def _attribute_score(
        self,
        query_colors: list[str],
        candidate_track_id: str,
    ) -> float:
        """Check colour attribute consistency. Returns 0.0 to 1.0."""
        if not query_colors:
            return 0.5  # No attributes — neutral

        candidate_colors = await self._get_colors(candidate_track_id)
        if not candidate_colors:
            return 0.5  # No attributes — neutral

        # Check overlap
        query_set = set(query_colors)
        candidate_set = set(candidate_colors)
        overlap = query_set & candidate_set
        if overlap:
            return 1.0
        return 0.0

    def _assign(
        self,
        scored: list[tuple[SearchResult, float]],
    ) -> Optional[tuple[SearchResult, float]]:
        """Select the best match: greedy for < 10, Hungarian for >= 10."""
        if not scored:
            return None

        if len(scored) < 10:
            # Greedy: pick the best
            return max(scored, key=lambda x: x[1])

        # Hungarian assignment (1 query vs N candidates)
        # Build cost matrix: 1 row (query) x N columns (candidates)
        scores = np.array([s for _, s in scored], dtype=np.float64)
        cost_matrix = scores.reshape(1, -1)
        # linear_sum_assignment minimises, so negate for maximisation
        row_ind, col_ind = linear_sum_assignment(-cost_matrix)
        best_col = col_ind[0]
        return scored[best_col]

    async def _persist_match(
        self,
        query_local_track_id: str,
        query_camera_id: str,
        query_object_class: str,
        match: tuple[SearchResult, float],
        query_timestamp: float,
    ) -> MatchResult:
        """Create or update global track and link."""
        result, combined_score = match
        matched_meta = result.meta
        now = datetime.fromtimestamp(query_timestamp, tz=timezone.utc)

        # Check if the matched track already belongs to a global track
        existing_gt = await self._db.find_existing_global_track(
            matched_meta.local_track_id
        )

        if existing_gt is not None:
            # Add query track to the existing global track
            global_track_id = existing_gt
            await self._db.update_global_track_last_seen(global_track_id, now)
        else:
            # Create a new global track
            first_seen = datetime.fromtimestamp(
                min(query_timestamp, matched_meta.timestamp), tz=timezone.utc
            )
            global_track_id = await self._db.create_global_track(
                query_object_class, first_seen, now,
            )
            # Link the matched track too
            await self._db.create_global_track_link(
                global_track_id,
                UUID(matched_meta.local_track_id),
                matched_meta.camera_id,
                combined_score,
                now,
            )

        # Link the query track
        await self._db.create_global_track_link(
            global_track_id,
            UUID(query_local_track_id),
            query_camera_id,
            combined_score,
            now,
        )

        logger.info(
            "Match: %s@%s -> %s@%s (score=%.3f, global=%s)",
            query_local_track_id,
            query_camera_id,
            matched_meta.local_track_id,
            matched_meta.camera_id,
            combined_score,
            global_track_id,
        )

        return MatchResult(
            query_local_track_id=query_local_track_id,
            matched_local_track_id=matched_meta.local_track_id,
            matched_camera_id=matched_meta.camera_id,
            global_track_id=global_track_id,
            combined_score=combined_score,
            cosine_score=result.score,
            transit_score=self._transit_likelihood(
                query_camera_id,
                matched_meta.camera_id,
                query_object_class,
                query_timestamp,
                matched_meta.timestamp,
            ),
            attribute_score=0.0,  # Computed in pipeline but not stored per-candidate
        )

    async def _get_track_info(self, local_track_id: str) -> Optional[tuple[str, str]]:
        """Get (camera_id, object_class) with caching."""
        if local_track_id in self._track_info_cache:
            return self._track_info_cache[local_track_id]
        info = await self._db.get_local_track_info(local_track_id)
        if info is not None:
            self._track_info_cache[local_track_id] = info
        return info

    async def _get_colors(self, local_track_id: str) -> list[str]:
        """Get colour attributes with caching."""
        if local_track_id in self._color_cache:
            return self._color_cache[local_track_id]
        colors = await self._db.get_track_colors(local_track_id)
        self._color_cache[local_track_id] = colors
        return colors
