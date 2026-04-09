"""Tests for the MTMC matcher scoring pipeline.

Creates synthetic 512-d L2-normalised embeddings with known matches and
verifies the matcher returns correct associations, respects version
boundaries, and enforces class consistency.
"""

from __future__ import annotations

import time
from typing import Optional
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import numpy as np
import pytest

from helpers import make_l2_normalised, make_similar_vector
from faiss_index import FAISSIndex
from matcher import Matcher


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_db_writer(
    track_map: dict[str, tuple[str, str]],
    color_map: dict[str, list[str]] | None = None,
    global_tracks: dict[str, UUID] | None = None,
) -> MagicMock:
    """Create a mock DBWriter with pre-loaded track info."""
    writer = MagicMock()

    async def get_track_info(local_track_id: str) -> Optional[tuple[str, str]]:
        return track_map.get(local_track_id)

    async def get_colors(local_track_id: str) -> list[str]:
        if color_map is None:
            return []
        return color_map.get(local_track_id, [])

    async def find_existing(local_track_id: str) -> Optional[UUID]:
        if global_tracks is None:
            return None
        return global_tracks.get(local_track_id)

    async def create_global_track(
        object_class: str, first_seen: object, last_seen: object,
    ) -> UUID:
        return uuid4()

    async def create_link(
        gt_id: UUID, lt_id: UUID, cam: str, conf: float, linked: object,
    ) -> UUID:
        return uuid4()

    async def update_last_seen(gt_id: UUID, last_seen: object) -> None:
        pass

    writer.get_local_track_info = AsyncMock(side_effect=get_track_info)
    writer.get_track_colors = AsyncMock(side_effect=get_colors)
    writer.find_existing_global_track = AsyncMock(side_effect=find_existing)
    writer.create_global_track = AsyncMock(side_effect=create_global_track)
    writer.create_global_track_link = AsyncMock(side_effect=create_link)
    writer.update_global_track_last_seen = AsyncMock(side_effect=update_last_seen)
    return writer


def _make_topo_client(
    candidate_cameras: set[str] | None = None,
    edge_dist: tuple[float, float, float] | None = None,
) -> MagicMock:
    """Create a mock TopologyClient."""
    topo = MagicMock()
    topo.get_candidates.return_value = candidate_cameras or set()

    if edge_dist is not None:
        topo.get_edge_distribution.return_value = edge_dist
    else:
        topo.get_edge_distribution.return_value = (5000.0, 7500.0, 12500.0)

    return topo


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_same_identity_matches(faiss_index: FAISSIndex, rng: np.random.Generator) -> None:
    """Embeddings from the same identity (cosine > 0.9) should match."""
    base_vector = make_l2_normalised(rng=rng)

    # Track on camera A
    track_a = str(uuid4())
    # Track on camera B — same person, similar embedding
    track_b = str(uuid4())
    similar_vector = make_similar_vector(base_vector, similarity=0.95, rng=rng)

    track_map = {
        track_a: ("cam-A", "person"),
        track_b: ("cam-B", "person"),
    }
    writer = _make_db_writer(track_map)
    topo = _make_topo_client(candidate_cameras={"cam-A", "cam-B"})

    matcher = Matcher(
        faiss_index=faiss_index,
        topology_client=topo,
        db_writer=writer,
        site_id="test",
        match_threshold=0.5,
    )

    now = time.time()

    # Add first track (gallery)
    result_a = await matcher.process_embedding(
        embedding_id=str(uuid4()),
        local_track_id=track_a,
        vector=base_vector,
        model_version="1.0.0",
        quality_score=0.9,
        timestamp=now - 5,
    )
    # First embedding — nothing to match against
    assert result_a is None

    # Add second track — should match with first
    result_b = await matcher.process_embedding(
        embedding_id=str(uuid4()),
        local_track_id=track_b,
        vector=similar_vector,
        model_version="1.0.0",
        quality_score=0.9,
        timestamp=now,
    )

    assert result_b is not None
    assert result_b.matched_local_track_id == track_a
    assert result_b.combined_score >= 0.5


@pytest.mark.asyncio
async def test_different_identity_no_match(faiss_index: FAISSIndex, rng: np.random.Generator) -> None:
    """Embeddings from different identities (cosine < 0.5) should NOT match."""
    vec_a = make_l2_normalised(rng=rng)
    vec_b = make_l2_normalised(rng=rng)
    # Ensure low similarity by using independent random vectors
    # (expected cosine ~ 0 for 512-d)

    track_a = str(uuid4())
    track_b = str(uuid4())

    track_map = {
        track_a: ("cam-A", "person"),
        track_b: ("cam-B", "person"),
    }
    writer = _make_db_writer(track_map)
    topo = _make_topo_client(candidate_cameras={"cam-A", "cam-B"})

    matcher = Matcher(
        faiss_index=faiss_index,
        topology_client=topo,
        db_writer=writer,
        site_id="test",
        match_threshold=0.65,
    )

    now = time.time()
    await matcher.process_embedding(
        embedding_id=str(uuid4()),
        local_track_id=track_a,
        vector=vec_a,
        model_version="1.0.0",
        quality_score=0.9,
        timestamp=now - 5,
    )

    result = await matcher.process_embedding(
        embedding_id=str(uuid4()),
        local_track_id=track_b,
        vector=vec_b,
        model_version="1.0.0",
        quality_score=0.9,
        timestamp=now,
    )

    # Random 512-d vectors have near-zero cosine → below threshold
    assert result is None


@pytest.mark.asyncio
async def test_version_boundary_rejection(faiss_index: FAISSIndex, rng: np.random.Generator) -> None:
    """Embeddings with different model versions must NEVER match."""
    base = make_l2_normalised(rng=rng)
    similar = make_similar_vector(base, similarity=0.99, rng=rng)

    track_a = str(uuid4())
    track_b = str(uuid4())

    track_map = {
        track_a: ("cam-A", "person"),
        track_b: ("cam-B", "person"),
    }
    writer = _make_db_writer(track_map)
    topo = _make_topo_client(candidate_cameras={"cam-A", "cam-B"})

    matcher = Matcher(
        faiss_index=faiss_index,
        topology_client=topo,
        db_writer=writer,
        site_id="test",
        match_threshold=0.3,  # Very low threshold — should still reject
    )

    now = time.time()
    await matcher.process_embedding(
        embedding_id=str(uuid4()),
        local_track_id=track_a,
        vector=base,
        model_version="1.0.0",
        quality_score=0.9,
        timestamp=now - 5,
    )

    # Different model version — must not match even with 0.99 cosine
    result = await matcher.process_embedding(
        embedding_id=str(uuid4()),
        local_track_id=track_b,
        vector=similar,
        model_version="2.0.0",
        quality_score=0.9,
        timestamp=now,
    )

    assert result is None


@pytest.mark.asyncio
async def test_class_consistency_rejection(faiss_index: FAISSIndex, rng: np.random.Generator) -> None:
    """Embeddings with different object classes must NOT match."""
    base = make_l2_normalised(rng=rng)
    similar = make_similar_vector(base, similarity=0.95, rng=rng)

    track_a = str(uuid4())
    track_b = str(uuid4())

    track_map = {
        track_a: ("cam-A", "person"),
        track_b: ("cam-B", "car"),  # Different class
    }
    writer = _make_db_writer(track_map)
    topo = _make_topo_client(candidate_cameras={"cam-A", "cam-B"})

    matcher = Matcher(
        faiss_index=faiss_index,
        topology_client=topo,
        db_writer=writer,
        site_id="test",
        match_threshold=0.3,
    )

    now = time.time()
    await matcher.process_embedding(
        embedding_id=str(uuid4()),
        local_track_id=track_a,
        vector=base,
        model_version="1.0.0",
        quality_score=0.9,
        timestamp=now - 5,
    )

    result = await matcher.process_embedding(
        embedding_id=str(uuid4()),
        local_track_id=track_b,
        vector=similar,
        model_version="1.0.0",
        quality_score=0.9,
        timestamp=now,
    )

    assert result is None


@pytest.mark.asyncio
async def test_topology_unreachable_rejection(
    faiss_index: FAISSIndex, rng: np.random.Generator
) -> None:
    """Embeddings from cameras not in topology should NOT match."""
    base = make_l2_normalised(rng=rng)
    similar = make_similar_vector(base, similarity=0.95, rng=rng)

    track_a = str(uuid4())
    track_b = str(uuid4())

    track_map = {
        track_a: ("cam-A", "person"),
        track_b: ("cam-B", "person"),
    }
    writer = _make_db_writer(track_map)
    # cam-B is NOT in the candidate set
    topo = _make_topo_client(candidate_cameras={"cam-C"})

    matcher = Matcher(
        faiss_index=faiss_index,
        topology_client=topo,
        db_writer=writer,
        site_id="test",
        match_threshold=0.3,
    )

    now = time.time()
    await matcher.process_embedding(
        embedding_id=str(uuid4()),
        local_track_id=track_a,
        vector=base,
        model_version="1.0.0",
        quality_score=0.9,
        timestamp=now - 5,
    )

    result = await matcher.process_embedding(
        embedding_id=str(uuid4()),
        local_track_id=track_b,
        vector=similar,
        model_version="1.0.0",
        quality_score=0.9,
        timestamp=now,
    )

    assert result is None


@pytest.mark.asyncio
async def test_multiple_matches_best_wins(
    faiss_index: FAISSIndex, rng: np.random.Generator
) -> None:
    """With multiple candidates, the best scoring one should be selected."""
    base = make_l2_normalised(rng=rng)

    tracks = []
    for i in range(5):
        tid = str(uuid4())
        tracks.append(tid)

    track_map = {
        tracks[0]: ("cam-A", "person"),
        tracks[1]: ("cam-B", "person"),
        tracks[2]: ("cam-C", "person"),
        tracks[3]: ("cam-D", "person"),
        tracks[4]: ("cam-E", "person"),
    }
    writer = _make_db_writer(track_map)
    topo = _make_topo_client(
        candidate_cameras={"cam-A", "cam-B", "cam-C", "cam-D", "cam-E"},
    )

    matcher = Matcher(
        faiss_index=faiss_index,
        topology_client=topo,
        db_writer=writer,
        site_id="test",
        match_threshold=0.3,
    )

    now = time.time()
    similarities = [0.7, 0.8, 0.95, 0.6]  # Index 2 (cam-C) is best

    for i, sim in enumerate(similarities):
        vec = make_similar_vector(base, similarity=sim, rng=rng)
        await matcher.process_embedding(
            embedding_id=str(uuid4()),
            local_track_id=tracks[i],
            vector=vec,
            model_version="1.0.0",
            quality_score=0.9,
            timestamp=now - 10 + i,
        )

    # Query from cam-E with a vector very similar to base
    query_vec = make_similar_vector(base, similarity=0.96, rng=rng)
    result = await matcher.process_embedding(
        embedding_id=str(uuid4()),
        local_track_id=tracks[4],
        vector=query_vec,
        model_version="1.0.0",
        quality_score=0.9,
        timestamp=now,
    )

    assert result is not None
    # Best match should be tracks[2] (0.95 similarity to base, highest cosine)
    assert result.matched_local_track_id == tracks[2]


@pytest.mark.asyncio
async def test_color_attribute_bonus(
    faiss_index: FAISSIndex, rng: np.random.Generator
) -> None:
    """Matching colour attributes should produce a higher score."""
    base = make_l2_normalised(rng=rng)

    track_a = str(uuid4())
    track_b = str(uuid4())

    track_map = {
        track_a: ("cam-A", "car"),
        track_b: ("cam-B", "car"),
    }
    color_map = {
        track_a: ["red"],
        track_b: ["red"],
    }
    writer = _make_db_writer(track_map, color_map=color_map)
    topo = _make_topo_client(candidate_cameras={"cam-A", "cam-B"})

    matcher = Matcher(
        faiss_index=faiss_index,
        topology_client=topo,
        db_writer=writer,
        site_id="test",
        match_threshold=0.3,
    )

    now = time.time()
    similar = make_similar_vector(base, similarity=0.9, rng=rng)

    await matcher.process_embedding(
        embedding_id=str(uuid4()),
        local_track_id=track_a,
        vector=base,
        model_version="1.0.0",
        quality_score=0.9,
        timestamp=now - 5,
    )

    result = await matcher.process_embedding(
        embedding_id=str(uuid4()),
        local_track_id=track_b,
        vector=similar,
        model_version="1.0.0",
        quality_score=0.9,
        timestamp=now,
    )

    assert result is not None
    assert result.combined_score > 0.5


@pytest.mark.asyncio
async def test_ten_embeddings_with_known_pairs(
    faiss_index: FAISSIndex, rng: np.random.Generator
) -> None:
    """10 synthetic embeddings with 5 known identity pairs."""
    identities = [make_l2_normalised(rng=rng) for _ in range(5)]
    tracks = [str(uuid4()) for _ in range(10)]
    cameras = ["cam-A", "cam-B", "cam-C", "cam-D", "cam-E"]

    # Pairs: (0,5), (1,6), (2,7), (3,8), (4,9) — same identity
    track_map = {}
    for i in range(5):
        track_map[tracks[i]] = (cameras[i % 5], "person")
        track_map[tracks[i + 5]] = (cameras[(i + 1) % 5], "person")

    writer = _make_db_writer(track_map)
    topo = _make_topo_client(candidate_cameras=set(cameras))

    matcher = Matcher(
        faiss_index=faiss_index,
        topology_client=topo,
        db_writer=writer,
        site_id="test",
        match_threshold=0.4,
    )

    now = time.time()

    # Add gallery embeddings (first 5)
    for i in range(5):
        await matcher.process_embedding(
            embedding_id=str(uuid4()),
            local_track_id=tracks[i],
            vector=identities[i],
            model_version="1.0.0",
            quality_score=0.9,
            timestamp=now - 10 + i,
        )

    # Query with matching identities (next 5)
    matched_count = 0
    for i in range(5):
        similar = make_similar_vector(identities[i], similarity=0.93, rng=rng)
        result = await matcher.process_embedding(
            embedding_id=str(uuid4()),
            local_track_id=tracks[i + 5],
            vector=similar,
            model_version="1.0.0",
            quality_score=0.9,
            timestamp=now + i,
        )
        if result is not None and result.matched_local_track_id == tracks[i]:
            matched_count += 1

    # At least 4 out of 5 should match correctly
    assert matched_count >= 4, f"Only {matched_count}/5 correct matches"
