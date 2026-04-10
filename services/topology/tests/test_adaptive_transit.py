"""Tests for adaptive transit-time learning.

Covers:
- blend_distribution with 0, partial, and full sample counts
- blend_distribution preserves object_class and sets last_updated
- Full pipeline with FakePool mock
- Transit-stats API endpoint
"""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient

from adaptive_transit import (
    LearnedDistribution,
    blend_distribution,
    fetch_learned_distributions,
    refresh_transit_stats,
    update_edge_distributions,
)
from models import TransitTimeDistribution, TransitionEdge


# ---------------------------------------------------------------------------
# blend_distribution tests
# ---------------------------------------------------------------------------


class TestBlendDistribution:
    def test_zero_samples_returns_prior(self) -> None:
        """0 samples -> weight = 0 -> prior returned unchanged."""
        prior = TransitTimeDistribution(
            object_class="person",
            p50_ms=5000.0,
            p90_ms=7500.0,
            p99_ms=12500.0,
            sample_count=0,
        )
        learned = LearnedDistribution(
            from_camera="cam-a",
            to_camera="cam-b",
            object_class="person",
            p50_ms=3000.0,
            p90_ms=4500.0,
            p99_ms=7500.0,
            sample_count=0,
        )
        result = blend_distribution(prior, learned, min_samples=100)

        assert result.p50_ms == prior.p50_ms
        assert result.p90_ms == prior.p90_ms
        assert result.p99_ms == prior.p99_ms
        assert result.sample_count == 0
        assert result.object_class == "person"

    def test_half_samples_blends_50_50(self) -> None:
        """50 / 100 samples -> weight = 0.5 -> midpoint."""
        prior = TransitTimeDistribution(
            object_class="car",
            p50_ms=2000.0,
            p90_ms=3000.0,
            p99_ms=5000.0,
        )
        learned = LearnedDistribution(
            from_camera="cam-a",
            to_camera="cam-b",
            object_class="car",
            p50_ms=1000.0,
            p90_ms=1500.0,
            p99_ms=2500.0,
            sample_count=50,
        )
        result = blend_distribution(prior, learned, min_samples=100)

        assert result.p50_ms == 1500.0  # (2000 * 0.5) + (1000 * 0.5)
        assert result.p90_ms == 2250.0  # (3000 * 0.5) + (1500 * 0.5)
        assert result.p99_ms == 3750.0  # (5000 * 0.5) + (2500 * 0.5)
        assert result.sample_count == 50

    def test_full_samples_returns_learned(self) -> None:
        """100+ samples -> weight = 1.0 -> learned returned."""
        prior = TransitTimeDistribution(
            object_class="truck",
            p50_ms=4000.0,
            p90_ms=6000.0,
            p99_ms=10000.0,
        )
        learned = LearnedDistribution(
            from_camera="cam-a",
            to_camera="cam-b",
            object_class="truck",
            p50_ms=3200.0,
            p90_ms=4800.0,
            p99_ms=8000.0,
            sample_count=150,
        )
        result = blend_distribution(prior, learned, min_samples=100)

        assert result.p50_ms == 3200.0
        assert result.p90_ms == 4800.0
        assert result.p99_ms == 8000.0
        assert result.sample_count == 150

    def test_preserves_object_class_and_sets_last_updated(self) -> None:
        """Result has correct object_class and a non-None last_updated."""
        prior = TransitTimeDistribution(
            object_class="bicycle",
            p50_ms=6000.0,
            p90_ms=9000.0,
            p99_ms=15000.0,
        )
        learned = LearnedDistribution(
            from_camera="cam-x",
            to_camera="cam-y",
            object_class="bicycle",
            p50_ms=5500.0,
            p90_ms=8000.0,
            p99_ms=13000.0,
            sample_count=75,
        )
        result = blend_distribution(prior, learned, min_samples=100)

        assert result.object_class == "bicycle"
        assert result.last_updated is not None
        assert result.last_updated.tzinfo is not None  # must be tz-aware

    def test_exactly_min_samples_gives_weight_one(self) -> None:
        """Exactly min_samples -> weight = 1.0."""
        prior = TransitTimeDistribution(
            object_class="person",
            p50_ms=10000.0,
            p90_ms=15000.0,
            p99_ms=25000.0,
        )
        learned = LearnedDistribution(
            from_camera="cam-a",
            to_camera="cam-b",
            object_class="person",
            p50_ms=8000.0,
            p90_ms=12000.0,
            p99_ms=20000.0,
            sample_count=100,
        )
        result = blend_distribution(prior, learned, min_samples=100)

        assert result.p50_ms == 8000.0
        assert result.p90_ms == 12000.0
        assert result.p99_ms == 20000.0


# ---------------------------------------------------------------------------
# Full pipeline test with mock DB
# ---------------------------------------------------------------------------


class _FakeConn:
    """Minimal async connection mock for the pipeline test."""

    def __init__(
        self,
        transit_stats_rows: list[dict],
        edge_rows: list[dict],
    ) -> None:
        self._transit_stats_rows = transit_stats_rows
        self._edge_rows = edge_rows
        self.executed: list[tuple[str, tuple]] = []

    async def execute(self, query: str, *args: object) -> None:
        self.executed.append((query, args))

    async def fetch(self, query: str, *args: object) -> list[dict]:
        if "transit_time_stats" in query:
            return self._transit_stats_rows
        if "topology_edges" in query:
            return self._edge_rows
        return []


@pytest.mark.asyncio
async def test_pipeline_integration() -> None:
    """Full pipeline: refresh -> fetch -> blend -> update."""
    edge_id = str(uuid.uuid4())

    transit_rows = [
        {
            "from_camera": "cam-a",
            "to_camera": "cam-b",
            "object_class": "person",
            "p50_ms": 4000.0,
            "p90_ms": 6000.0,
            "p99_ms": 10000.0,
            "sample_count": 200,
        },
    ]
    edge_rows = [
        {
            "edge_id": edge_id,
            "camera_a_id": "cam-a",
            "camera_b_id": "cam-b",
            "transition_time_s": 5.0,
        },
    ]
    conn = _FakeConn(transit_rows, edge_rows)

    # Step 1: refresh
    await refresh_transit_stats(conn)
    assert any("REFRESH" in q for q, _ in conn.executed)

    # Step 2: fetch
    learned = await fetch_learned_distributions(conn)
    assert len(learned) == 1
    assert learned[0].object_class == "person"
    assert learned[0].sample_count == 200

    # Step 3: blend all classes for this edge
    priors = TransitionEdge.default_distributions(5.0)
    prior_by_class = {d.object_class: d for d in priors}

    learned_by_class = {r.object_class: r for r in learned}
    blended = []
    for obj_class, prior in prior_by_class.items():
        lr = learned_by_class.get(obj_class)
        if lr:
            blended.append(blend_distribution(prior, lr, min_samples=100))
        else:
            blended.append(prior)

    # Person should be fully learned (200 >= 100)
    person = next(d for d in blended if d.object_class == "person")
    assert person.p50_ms == 4000.0
    assert person.sample_count == 200

    # Car should still be the prior (no learned data)
    car = next(d for d in blended if d.object_class == "car")
    assert car.p50_ms == 1500.0  # 5.0 * 1000 * 0.3
    assert car.sample_count == 0

    # Step 4: update
    await update_edge_distributions(conn, edge_id, blended)
    update_calls = [q for q, _ in conn.executed if "UPDATE" in q]
    assert len(update_calls) == 1


# ---------------------------------------------------------------------------
# API endpoint test
# ---------------------------------------------------------------------------


class TestTransitStatsAPI:
    @pytest.mark.asyncio
    async def test_returns_stats(
        self, client: AsyncClient, make_jwt, fake_pool,
    ) -> None:
        fake_pool.set_rows([
            {
                "from_camera": "cam-1",
                "to_camera": "cam-2",
                "object_class": "person",
                "p50_ms": 4500.0,
                "p90_ms": 6750.0,
                "p99_ms": 11250.0,
                "sample_count": 50,
            },
        ])
        token = make_jwt(role="operator")
        resp = await client.get(
            "/topology/site-1/transit-stats",
            cookies={"access_token": token},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 1
        assert data[0]["from_camera"] == "cam-1"
        assert data[0]["object_class"] == "person"
        assert data[0]["blend_weight"] == 0.5  # 50 / 100

    @pytest.mark.asyncio
    async def test_custom_min_samples(
        self, client: AsyncClient, make_jwt, fake_pool,
    ) -> None:
        fake_pool.set_rows([
            {
                "from_camera": "cam-1",
                "to_camera": "cam-2",
                "object_class": "person",
                "p50_ms": 4500.0,
                "p90_ms": 6750.0,
                "p99_ms": 11250.0,
                "sample_count": 50,
            },
        ])
        token = make_jwt(role="admin")
        resp = await client.get(
            "/topology/site-1/transit-stats?min_samples=50",
            cookies={"access_token": token},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data[0]["blend_weight"] == 1.0  # 50 / 50

    @pytest.mark.asyncio
    async def test_viewer_forbidden(
        self, client: AsyncClient, make_jwt,
    ) -> None:
        token = make_jwt(role="viewer")
        resp = await client.get(
            "/topology/site-1/transit-stats",
            cookies={"access_token": token},
        )
        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_empty_stats(
        self, client: AsyncClient, make_jwt, fake_pool,
    ) -> None:
        fake_pool.set_rows([])
        token = make_jwt(role="admin")
        resp = await client.get(
            "/topology/site-1/transit-stats",
            cookies={"access_token": token},
        )
        assert resp.status_code == 200
        assert resp.json() == []
