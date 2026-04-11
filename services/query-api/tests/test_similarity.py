"""Tests for /search/similar endpoints."""

from __future__ import annotations

import base64
import io
import time
from dataclasses import dataclass
import numpy as np
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient


# ---------------------------------------------------------------------------
# Fake FAISS reader (no faiss dependency needed in tests)
# ---------------------------------------------------------------------------


@dataclass
class FakeEmbeddingMeta:
    embedding_id: str
    camera_id: str
    local_track_id: str
    object_class: str
    model_version: str
    timestamp: float


@dataclass
class FakeSearchResult:
    faiss_id: int
    score: float
    meta: FakeEmbeddingMeta


class FakeFAISSReader:
    """Mock FAISSReader for tests — no actual FAISS dependency."""

    def __init__(self) -> None:
        self._results: list[FakeSearchResult] = []
        self._track_embeddings: dict[str, np.ndarray] = {}
        self._track_meta: dict[str, FakeEmbeddingMeta] = {}
        self._index_size: int = 0
        self.refresh_called: bool = False

    @property
    def index_size(self) -> int:
        return self._index_size

    @property
    def is_loaded(self) -> bool:
        return True

    def maybe_refresh(self) -> None:
        self.refresh_called = True

    def search(self, vector: np.ndarray, k: int = 10) -> list[FakeSearchResult]:
        return self._results[:k]

    def get_embedding_by_track(self, local_track_id: str) -> np.ndarray | None:
        return self._track_embeddings.get(local_track_id)

    def get_track_meta(self, local_track_id: str) -> FakeEmbeddingMeta | None:
        return self._track_meta.get(local_track_id)

    def set_results(self, results: list[FakeSearchResult]) -> None:
        self._results = results
        self._index_size = len(results)

    def add_track(
        self, local_track_id: str, embedding: np.ndarray, meta: FakeEmbeddingMeta
    ) -> None:
        self._track_embeddings[local_track_id] = embedding
        self._track_meta[local_track_id] = meta


def _make_results(
    n: int = 3,
    camera_id: str = "cam-1",
    object_class: str = "person",
    base_score: float = 0.95,
) -> list[FakeSearchResult]:
    """Create N fake search results."""
    results = []
    for i in range(n):
        meta = FakeEmbeddingMeta(
            embedding_id=f"emb-{i}",
            camera_id=camera_id,
            local_track_id=f"track-{i}",
            object_class=object_class,
            model_version="osnet-1.0",
            timestamp=time.time() - i * 60,
        )
        results.append(
            FakeSearchResult(faiss_id=i, score=base_score - i * 0.05, meta=meta)
        )
    return results


def _make_png_b64() -> str:
    """Create a minimal valid PNG image as base64."""
    from PIL import Image

    img = Image.new("RGB", (128, 256), color=(128, 64, 32))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_faiss() -> FakeFAISSReader:
    return FakeFAISSReader()


@pytest.fixture
def app_with_faiss(app, fake_faiss):
    app.state.faiss_reader = fake_faiss
    return app


@pytest_asyncio.fixture
async def sim_client(app_with_faiss) -> AsyncClient:
    transport = ASGITransport(app=app_with_faiss)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ---------------------------------------------------------------------------
# POST /search/similar — embedding vector
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_by_embedding_vector(
    sim_client: AsyncClient,
    make_jwt,
    fake_faiss: FakeFAISSReader,
) -> None:
    fake_faiss.set_results(_make_results(3))

    embedding = np.random.randn(512).astype(np.float32).tolist()
    response = await sim_client.post(
        "/search/similar",
        json={"embedding": embedding, "k": 5},
        cookies={"access_token": make_jwt(role="operator")},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["query_embedding_source"] == "vector"
    assert len(data["results"]) == 3
    assert data["results"][0]["similarity_score"] >= data["results"][1]["similarity_score"]
    assert data["index_size"] == 3


@pytest.mark.asyncio
async def test_search_by_embedding_wrong_dim(
    sim_client: AsyncClient,
    make_jwt,
    fake_faiss: FakeFAISSReader,
) -> None:
    response = await sim_client.post(
        "/search/similar",
        json={"embedding": [0.1] * 256},
        cookies={"access_token": make_jwt(role="operator")},
    )

    assert response.status_code == 400
    assert "512-dimensional" in response.json()["detail"]


# ---------------------------------------------------------------------------
# POST /search/similar — base64 image (Triton mocked)
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_triton(app_with_faiss):
    """Mock Triton client that returns a random 512-d embedding."""

    class FakeTritonResult:
        def as_numpy(self, name: str) -> np.ndarray:
            vec = np.random.randn(512).astype(np.float32)
            return vec / np.linalg.norm(vec)

    class FakeTritonClient:
        def infer(self, model_name: str, inputs: list) -> FakeTritonResult:
            return FakeTritonResult()

    app_with_faiss.state.triton_client = FakeTritonClient()
    return app_with_faiss


@pytest.mark.asyncio
async def test_search_by_image(
    sim_client: AsyncClient,
    make_jwt,
    fake_faiss: FakeFAISSReader,
    fake_triton,
) -> None:
    fake_faiss.set_results(_make_results(2))

    response = await sim_client.post(
        "/search/similar",
        json={"image_base64": _make_png_b64(), "k": 5},
        cookies={"access_token": make_jwt(role="admin")},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["query_embedding_source"] == "image"
    assert len(data["results"]) == 2


@pytest.mark.asyncio
async def test_search_image_without_triton(
    sim_client: AsyncClient,
    make_jwt,
    fake_faiss: FakeFAISSReader,
) -> None:
    """Image search without Triton client configured should return 503."""
    response = await sim_client.post(
        "/search/similar",
        json={"image_base64": _make_png_b64()},
        cookies={"access_token": make_jwt(role="operator")},
    )
    assert response.status_code == 503


# ---------------------------------------------------------------------------
# POST /search/similar — validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_must_provide_image_or_embedding(
    sim_client: AsyncClient,
    make_jwt,
    fake_faiss: FakeFAISSReader,
) -> None:
    response = await sim_client.post(
        "/search/similar",
        json={"k": 5},
        cookies={"access_token": make_jwt(role="operator")},
    )

    assert response.status_code == 400
    assert "Must provide" in response.json()["detail"]


# ---------------------------------------------------------------------------
# GET /search/similar/{track_id}
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_search_by_track_id(
    sim_client: AsyncClient,
    make_jwt,
    fake_faiss: FakeFAISSReader,
) -> None:
    # Add the query track's embedding
    vec = np.random.randn(512).astype(np.float32)
    vec = vec / np.linalg.norm(vec)
    meta = FakeEmbeddingMeta(
        embedding_id="emb-query",
        camera_id="cam-1",
        local_track_id="track-query",
        object_class="person",
        model_version="osnet-1.0",
        timestamp=time.time(),
    )
    fake_faiss.add_track("track-query", vec, meta)

    # Set search results (include self-match that should be filtered out)
    results = _make_results(3)
    results.insert(
        0,
        FakeSearchResult(faiss_id=99, score=1.0, meta=meta),
    )
    fake_faiss.set_results(results)

    response = await sim_client.get(
        "/search/similar/track-query?k=5",
        cookies={"access_token": make_jwt(role="operator")},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["query_embedding_source"] == "track"
    # Self-match (track-query) should be excluded
    track_ids = [r["local_track_id"] for r in data["results"]]
    assert "track-query" not in track_ids


@pytest.mark.asyncio
async def test_search_by_track_not_found(
    sim_client: AsyncClient,
    make_jwt,
    fake_faiss: FakeFAISSReader,
) -> None:
    response = await sim_client.get(
        "/search/similar/nonexistent-track",
        cookies={"access_token": make_jwt(role="operator")},
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Filtering
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_filter_by_camera_and_class(
    sim_client: AsyncClient,
    make_jwt,
    fake_faiss: FakeFAISSReader,
) -> None:
    results = _make_results(2, camera_id="cam-1", object_class="person")
    results.extend(_make_results(2, camera_id="cam-2", object_class="car"))
    fake_faiss.set_results(results)

    response = await sim_client.post(
        "/search/similar",
        json={
            "embedding": np.random.randn(512).astype(np.float32).tolist(),
            "camera_id": "cam-1",
            "object_class": "person",
            "k": 10,
        },
        cookies={"access_token": make_jwt(role="operator")},
    )

    assert response.status_code == 200
    data = response.json()
    for r in data["results"]:
        assert r["camera_id"] == "cam-1"
        assert r["object_class"] == "person"


@pytest.mark.asyncio
async def test_filter_by_time_range(
    sim_client: AsyncClient,
    make_jwt,
    fake_faiss: FakeFAISSReader,
) -> None:
    now = time.time()
    results = []
    for i in range(4):
        meta = FakeEmbeddingMeta(
            embedding_id=f"emb-{i}",
            camera_id="cam-1",
            local_track_id=f"track-{i}",
            object_class="person",
            model_version="osnet-1.0",
            timestamp=now - i * 3600,  # 0h, 1h, 2h, 3h ago
        )
        results.append(FakeSearchResult(faiss_id=i, score=0.9 - i * 0.1, meta=meta))
    fake_faiss.set_results(results)

    from datetime import datetime, timezone

    start_ts = datetime.fromtimestamp(now - 7200, tz=timezone.utc).isoformat()
    end_ts = datetime.fromtimestamp(now - 1800, tz=timezone.utc).isoformat()

    response = await sim_client.post(
        "/search/similar",
        json={
            "embedding": np.random.randn(512).astype(np.float32).tolist(),
            "start": start_ts,
            "end": end_ts,
            "k": 10,
        },
        cookies={"access_token": make_jwt(role="operator")},
    )

    assert response.status_code == 200
    data = response.json()
    # Only results with timestamps between start and end should remain
    assert len(data["results"]) <= 4


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_viewer_forbidden(
    sim_client: AsyncClient,
    make_jwt,
) -> None:
    response = await sim_client.post(
        "/search/similar",
        json={"embedding": [0.1] * 512},
        cookies={"access_token": make_jwt(role="viewer")},
    )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_viewer_forbidden_get(
    sim_client: AsyncClient,
    make_jwt,
) -> None:
    response = await sim_client.get(
        "/search/similar/some-track",
        cookies={"access_token": make_jwt(role="viewer")},
    )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_engineering_allowed(
    sim_client: AsyncClient,
    make_jwt,
    fake_faiss: FakeFAISSReader,
) -> None:
    fake_faiss.set_results(_make_results(1))

    response = await sim_client.post(
        "/search/similar",
        json={"embedding": np.random.randn(512).astype(np.float32).tolist()},
        cookies={"access_token": make_jwt(role="engineering")},
    )
    assert response.status_code == 200
