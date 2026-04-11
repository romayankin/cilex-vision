"""Visual similarity search endpoints.

POST /search/similar — search by image crop or embedding vector.
GET /search/similar/{local_track_id} — find tracks similar to a given track.
"""

from __future__ import annotations

import base64
import io
import logging
import time
from datetime import datetime, timezone
from typing import Optional

import numpy as np
from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from auth.jwt import get_current_user, require_role
from schemas import UserClaims

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/search", tags=["similarity"])

EMBEDDING_DIM = 512
_UTC = timezone.utc


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class SimilaritySearchRequest(BaseModel):
    image_base64: Optional[str] = None
    embedding: Optional[list[float]] = None
    k: int = Field(default=10, ge=1, le=100)
    camera_id: Optional[str] = None
    object_class: Optional[str] = None
    start: Optional[datetime] = None
    end: Optional[datetime] = None


class SimilarityResult(BaseModel):
    local_track_id: str
    camera_id: str
    object_class: str
    similarity_score: float
    timestamp: datetime
    embedding_id: str
    thumbnail_url: Optional[str] = None


class SimilaritySearchResponse(BaseModel):
    results: list[SimilarityResult]
    query_embedding_source: str
    index_size: int
    search_time_ms: float


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _l2_normalize(vec: np.ndarray) -> np.ndarray:
    """L2-normalize a vector so inner product = cosine similarity."""
    norm = np.linalg.norm(vec)
    if norm < 1e-12:
        return vec
    return vec / norm


def _decode_image_to_embedding(image_b64: str, request: Request) -> np.ndarray:
    """Decode base64 image, run OSNet Re-ID via Triton, return 512-d embedding."""
    try:
        from PIL import Image  # noqa: PLC0415
    except ImportError as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Pillow not installed — image search unavailable",
        ) from exc

    # Decode base64 → PIL image
    try:
        img_bytes = base64.b64decode(image_b64)
        img = Image.open(io.BytesIO(img_bytes)).convert("RGB")
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid base64 image: {exc}",
        ) from exc

    # Resize to OSNet input: 256×128 (H×W)
    img = img.resize((128, 256))

    # ImageNet normalization
    arr = np.array(img, dtype=np.float32) / 255.0
    mean = np.array([0.485, 0.456, 0.406], dtype=np.float32)
    std = np.array([0.229, 0.224, 0.225], dtype=np.float32)
    arr = (arr - mean) / std
    arr = arr.transpose(2, 0, 1)  # HWC → CHW
    arr = np.expand_dims(arr, 0)  # add batch dim → (1, 3, 256, 128)

    # Run Triton inference
    triton_client = getattr(request.app.state, "triton_client", None)
    if triton_client is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Triton inference client not configured",
        )

    try:
        import tritonclient.http as httpclient  # noqa: PLC0415

        inputs = [httpclient.InferInput("input", arr.shape, "FP32")]
        inputs[0].set_data_from_numpy(arr)

        settings = request.app.state.settings
        model_name = getattr(settings, "triton_reid_model", "osnet_reid")
        result = triton_client.infer(model_name, inputs)
        embedding = result.as_numpy("output").flatten()
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Triton inference failed: {exc}",
        ) from exc

    return _l2_normalize(embedding.astype(np.float32))


def _filter_results(
    results: list,
    camera_id: str | None,
    object_class: str | None,
    start: datetime | None,
    end: datetime | None,
) -> list:
    """Apply post-search filters on metadata."""
    filtered = []
    for r in results:
        meta = r.meta
        if camera_id and meta.camera_id != camera_id:
            continue
        if object_class and meta.object_class != object_class:
            continue
        if start and meta.timestamp < start.timestamp():
            continue
        if end and meta.timestamp >= end.timestamp():
            continue
        filtered.append(r)
    return filtered


def _build_response(
    results: list,
    source: str,
    index_size: int,
    search_time_ms: float,
    minio_client: object | None = None,
    expiry_s: int = 3600,
) -> SimilaritySearchResponse:
    """Convert SearchResult list to API response with optional thumbnail URLs."""
    from utils.minio_urls import generate_signed_url  # noqa: PLC0415

    items: list[SimilarityResult] = []
    for r in results:
        meta = r.meta
        # Build thumbnail URI from track metadata
        thumbnail_uri = f"s3://thumbnails/{meta.camera_id}/{meta.local_track_id}.jpg"
        thumbnail_url = generate_signed_url(minio_client, thumbnail_uri, expiry_s)

        items.append(
            SimilarityResult(
                local_track_id=meta.local_track_id,
                camera_id=meta.camera_id,
                object_class=meta.object_class,
                similarity_score=round(float(r.score), 4),
                timestamp=datetime.fromtimestamp(meta.timestamp, tz=_UTC),
                embedding_id=meta.embedding_id,
                thumbnail_url=thumbnail_url,
            )
        )

    return SimilaritySearchResponse(
        results=items,
        query_embedding_source=source,
        index_size=index_size,
        search_time_ms=round(search_time_ms, 2),
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/similar",
    response_model=SimilaritySearchResponse,
    dependencies=[require_role("admin", "operator", "engineering")],
)
async def search_similar(
    body: SimilaritySearchRequest,
    request: Request,
    user: UserClaims = Depends(get_current_user),
) -> SimilaritySearchResponse:
    """Search by image crop or embedding vector."""
    request.state.audit_user_id = user.user_id
    request.state.audit_username = user.username

    faiss_reader = getattr(request.app.state, "faiss_reader", None)
    if faiss_reader is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="FAISS index not available",
        )

    faiss_reader.maybe_refresh()

    if body.image_base64 is not None:
        query_vec = _decode_image_to_embedding(body.image_base64, request)
        source = "image"
    elif body.embedding is not None:
        if len(body.embedding) != EMBEDDING_DIM:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Embedding must be {EMBEDDING_DIM}-dimensional, got {len(body.embedding)}",
            )
        query_vec = _l2_normalize(np.array(body.embedding, dtype=np.float32))
        source = "vector"
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Must provide either image_base64 or embedding",
        )

    # Over-fetch to account for post-filtering
    fetch_k = min(body.k * 3, 300)

    t0 = time.monotonic()
    results = faiss_reader.search(query_vec, k=fetch_k)
    search_ms = (time.monotonic() - t0) * 1000

    results = _filter_results(
        results, body.camera_id, body.object_class, body.start, body.end
    )
    results = results[: body.k]

    minio_client = getattr(request.app.state, "minio_client", None)
    settings = request.app.state.settings
    expiry_s = settings.minio.signed_url_expiry_s

    return _build_response(results, source, faiss_reader.index_size, search_ms, minio_client, expiry_s)


@router.get(
    "/similar/{local_track_id}",
    response_model=SimilaritySearchResponse,
    dependencies=[require_role("admin", "operator", "engineering")],
)
async def search_similar_by_track(
    local_track_id: str,
    request: Request,
    k: int = Query(default=10, ge=1, le=100),
    camera_id: Optional[str] = Query(None, description="Filter by camera ID"),
    object_class: Optional[str] = Query(None, alias="class", description="Filter by object class"),
    start: Optional[datetime] = Query(None, description="Start time (inclusive)"),
    end: Optional[datetime] = Query(None, description="End time (exclusive)"),
    user: UserClaims = Depends(get_current_user),
) -> SimilaritySearchResponse:
    """Find tracks visually similar to a given track."""
    request.state.audit_user_id = user.user_id
    request.state.audit_username = user.username

    faiss_reader = getattr(request.app.state, "faiss_reader", None)
    if faiss_reader is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="FAISS index not available",
        )

    faiss_reader.maybe_refresh()

    query_vec = faiss_reader.get_embedding_by_track(local_track_id)
    if query_vec is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No embedding found for track {local_track_id}",
        )

    fetch_k = min((k + 1) * 3, 300)  # +1 to exclude self-match

    t0 = time.monotonic()
    results = faiss_reader.search(query_vec, k=fetch_k)
    search_ms = (time.monotonic() - t0) * 1000

    # Exclude the query track itself from results
    results = [r for r in results if r.meta.local_track_id != local_track_id]

    results = _filter_results(results, camera_id, object_class, start, end)
    results = results[:k]

    minio_client = getattr(request.app.state, "minio_client", None)
    settings = request.app.state.settings
    expiry_s = settings.minio.signed_url_expiry_s

    return _build_response(
        results, "track", faiss_reader.index_size, search_ms, minio_client, expiry_s
    )
