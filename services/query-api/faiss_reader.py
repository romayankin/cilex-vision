"""Read-only FAISS index loaded from MTMC checkpoint snapshot.

Loads the pickle checkpoint from MinIO, extracts the FAISS index and
metadata, and provides a thread-safe search interface without writing
to the live MTMC index.
"""

from __future__ import annotations

import logging
import pickle
import threading
import time
from dataclasses import dataclass
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)


@dataclass
class EmbeddingMeta:
    """Metadata stored alongside each indexed embedding.

    Mirrors services/mtmc-service/faiss_index.py EmbeddingMeta.
    """

    embedding_id: str
    camera_id: str
    local_track_id: str
    object_class: str
    model_version: str
    timestamp: float  # epoch seconds


@dataclass
class SearchResult:
    """Single FAISS search result with metadata."""

    faiss_id: int
    score: float
    meta: EmbeddingMeta


class FAISSReader:
    """Read-only FAISS index loaded from MTMC checkpoint snapshots.

    Thread-safe. Periodically refreshes from MinIO.
    """

    def __init__(
        self,
        minio_client: Any,
        checkpoint_bucket: str = "mtmc-checkpoints",
        site_id: str = "site-a",
        refresh_interval_s: float = 300.0,
    ) -> None:
        self._minio = minio_client
        self._bucket = checkpoint_bucket
        self._site_id = site_id
        self._refresh_interval_s = refresh_interval_s

        self._lock = threading.Lock()
        self._index: Any = None  # faiss.IndexIDMap
        self._metadata: dict[int, EmbeddingMeta] = {}
        self._track_map: dict[str, int] = {}
        self._last_refresh: float = 0.0
        self._embedding_count: int = 0

    @property
    def index_size(self) -> int:
        with self._lock:
            return self._embedding_count

    @property
    def is_loaded(self) -> bool:
        with self._lock:
            return self._index is not None

    def load(self) -> bool:
        """Load checkpoint from MinIO. Returns True on success."""
        checkpoint = self._fetch_checkpoint()
        if checkpoint is None:
            return False

        with self._lock:
            self._index = checkpoint.index
            self._metadata = checkpoint.metadata
            self._track_map = getattr(checkpoint, "track_map", {})
            self._embedding_count = checkpoint.embedding_count
            self._last_refresh = time.time()

        logger.info(
            "FAISS reader loaded: %d embeddings from checkpoint",
            checkpoint.embedding_count,
        )
        return True

    def maybe_refresh(self) -> None:
        """Refresh from MinIO if the refresh interval has elapsed."""
        if (time.time() - self._last_refresh) < self._refresh_interval_s:
            return
        try:
            self.load()
        except Exception:
            logger.warning("Checkpoint refresh failed", exc_info=True)

    def search(self, vector: np.ndarray, k: int = 10) -> list[SearchResult]:
        """Search for top-K nearest neighbours by inner product (cosine on L2-normed).

        Parameters
        ----------
        vector:
            Query embedding, 1-D float32 array (512-d). Must be L2-normalised.
        k:
            Number of results to return.

        Returns
        -------
        List of SearchResult sorted by score descending.
        """
        try:
            import faiss  # noqa: F401, PLC0415
        except ImportError:
            logger.error("faiss not installed — search unavailable")
            return []

        vec = np.ascontiguousarray(vector.reshape(1, -1), dtype=np.float32)

        with self._lock:
            if self._index is None or self._embedding_count == 0:
                return []
            effective_k = min(k, self._embedding_count)
            scores, ids = self._index.search(vec, effective_k)

        results: list[SearchResult] = []
        for score, fid in zip(scores[0], ids[0]):
            if fid == -1:
                continue
            meta = self._metadata.get(int(fid))
            if meta is None:
                continue
            results.append(
                SearchResult(faiss_id=int(fid), score=float(score), meta=meta)
            )
        return results

    def get_embedding_by_track(self, local_track_id: str) -> np.ndarray | None:
        """Look up the embedding vector for a track. Returns None if not found."""
        with self._lock:
            if self._index is None:
                return None
            fid = self._track_map.get(local_track_id)
            if fid is None:
                return None
            try:
                return self._index.reconstruct(int(fid))
            except RuntimeError:
                logger.warning("Failed to reconstruct FAISS ID %d", fid)
                return None

    def get_track_meta(self, local_track_id: str) -> EmbeddingMeta | None:
        """Look up metadata for a track."""
        with self._lock:
            fid = self._track_map.get(local_track_id)
            if fid is None:
                return None
            return self._metadata.get(int(fid))

    def _fetch_checkpoint(self) -> Any | None:
        """Fetch and deserialize the latest checkpoint from MinIO."""
        if self._minio is None:
            logger.warning("No MinIO client — FAISS reader cannot load checkpoint")
            return None

        key = f"{self._site_id}/latest.pkl"
        try:
            response = self._minio.get_object(self._bucket, key)
            payload = response.read()
            response.close()
            response.release_conn()
            data = pickle.loads(payload)  # noqa: S301
            return data
        except Exception:
            logger.info("No checkpoint available in MinIO (%s/%s)", self._bucket, key)
            return None
