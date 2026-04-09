"""FAISS index management for real-time Re-ID matching.

Uses ``faiss.IndexIDMap`` wrapping ``faiss.IndexFlatIP`` (inner product on
L2-normalised vectors = cosine similarity).  Maintains a metadata dict
mapping FAISS int64 IDs to embedding metadata for post-search filtering.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass

import faiss
import numpy as np

logger = logging.getLogger(__name__)


@dataclass
class EmbeddingMeta:
    """Metadata stored alongside each indexed embedding."""

    embedding_id: str
    camera_id: str
    local_track_id: str
    object_class: str
    model_version: str
    timestamp: float  # epoch seconds (edge_receive_ts)


@dataclass
class SearchResult:
    """Single FAISS search result with metadata."""

    faiss_id: int
    score: float
    meta: EmbeddingMeta


class FAISSIndex:
    """Thread-safe wrapper around FAISS IndexFlatIP with metadata tracking.

    All vectors MUST be L2-normalised before insertion so that inner product
    equals cosine similarity.
    """

    def __init__(self, dimension: int = 512, active_horizon_minutes: int = 30) -> None:
        self._dimension = dimension
        self._active_horizon_s = active_horizon_minutes * 60.0
        self._lock = threading.Lock()

        # Inner product on L2-normalised vectors = cosine similarity
        flat_index = faiss.IndexFlatIP(dimension)
        self._index: faiss.IndexIDMap = faiss.IndexIDMap(flat_index)

        # Metadata: faiss_id -> EmbeddingMeta
        self._metadata: dict[int, EmbeddingMeta] = {}
        # Reverse map: embedding_id -> faiss_id (for removal)
        self._id_map: dict[str, int] = {}
        # Reverse map: local_track_id -> faiss_id (for tombstone removal)
        self._track_map: dict[str, int] = {}
        # Monotonically increasing counter for faiss int64 IDs
        self._next_id: int = 0

    @property
    def dimension(self) -> int:
        return self._dimension

    def add(
        self,
        embedding_id: str,
        vector: np.ndarray,
        meta: EmbeddingMeta,
    ) -> int:
        """Add an embedding to the index. Returns the assigned FAISS ID."""
        vec = np.ascontiguousarray(vector.reshape(1, -1), dtype=np.float32)

        with self._lock:
            # Remove previous embedding for this track if exists
            old_id = self._track_map.get(meta.local_track_id)
            if old_id is not None:
                self._remove_by_faiss_id(old_id)

            fid = self._next_id
            self._next_id += 1

            ids = np.array([fid], dtype=np.int64)
            self._index.add_with_ids(vec, ids)
            self._metadata[fid] = meta
            self._id_map[embedding_id] = fid
            self._track_map[meta.local_track_id] = fid

        return fid

    def search(self, vector: np.ndarray, k: int = 20) -> list[SearchResult]:
        """Search for top-K nearest neighbours by inner product."""
        vec = np.ascontiguousarray(vector.reshape(1, -1), dtype=np.float32)

        with self._lock:
            n = self._index.ntotal
            if n == 0:
                return []
            effective_k = min(k, n)
            scores, ids = self._index.search(vec, effective_k)

        results: list[SearchResult] = []
        for score, fid in zip(scores[0], ids[0]):
            if fid == -1:
                continue
            meta = self._metadata.get(int(fid))
            if meta is None:
                continue
            results.append(SearchResult(faiss_id=int(fid), score=float(score), meta=meta))
        return results

    def remove(self, embedding_id: str) -> bool:
        """Remove an embedding by embedding_id. Returns True if found."""
        with self._lock:
            fid = self._id_map.get(embedding_id)
            if fid is None:
                return False
            self._remove_by_faiss_id(fid)
            return True

    def remove_by_track(self, local_track_id: str) -> bool:
        """Remove an embedding by local_track_id (used for tombstones)."""
        with self._lock:
            fid = self._track_map.get(local_track_id)
            if fid is None:
                return False
            self._remove_by_faiss_id(fid)
            return True

    def cleanup_expired(self) -> int:
        """Remove embeddings older than the active horizon. Returns count removed."""
        cutoff = time.time() - self._active_horizon_s
        to_remove: list[int] = []

        with self._lock:
            for fid, meta in list(self._metadata.items()):
                if meta.timestamp < cutoff:
                    to_remove.append(fid)
            for fid in to_remove:
                self._remove_by_faiss_id(fid)

        if to_remove:
            logger.info("Cleaned up %d expired embeddings", len(to_remove))
        return len(to_remove)

    def rebuild(self) -> None:
        """Rebuild the FAISS index from current metadata (compacts ID space)."""
        with self._lock:
            if not self._metadata:
                flat = faiss.IndexFlatIP(self._dimension)
                self._index = faiss.IndexIDMap(flat)
                return

            # Collect all current vectors via reconstruction
            fids = list(self._metadata.keys())
            vectors = np.zeros((len(fids), self._dimension), dtype=np.float32)
            for i, fid in enumerate(fids):
                try:
                    vectors[i] = self._index.reconstruct(fid)
                except RuntimeError:
                    logger.warning("Failed to reconstruct FAISS ID %d", fid)

            # Rebuild index
            flat = faiss.IndexFlatIP(self._dimension)
            new_index = faiss.IndexIDMap(flat)
            ids_arr = np.array(fids, dtype=np.int64)
            new_index.add_with_ids(vectors, ids_arr)
            self._index = new_index

    def size(self) -> int:
        """Return number of embeddings in the index."""
        with self._lock:
            return self._index.ntotal

    def flush(self) -> None:
        """Clear the entire index and all metadata."""
        with self._lock:
            flat = faiss.IndexFlatIP(self._dimension)
            self._index = faiss.IndexIDMap(flat)
            self._metadata.clear()
            self._id_map.clear()
            self._track_map.clear()

    def get_state(self) -> tuple[faiss.IndexIDMap, dict[int, EmbeddingMeta], dict[str, int], dict[str, int], int]:
        """Return index state for checkpointing."""
        with self._lock:
            return (
                self._index,
                dict(self._metadata),
                dict(self._id_map),
                dict(self._track_map),
                self._next_id,
            )

    def restore_state(
        self,
        index: faiss.IndexIDMap,
        metadata: dict[int, EmbeddingMeta],
        id_map: dict[str, int],
        track_map: dict[str, int],
        next_id: int,
    ) -> None:
        """Restore index state from a checkpoint."""
        with self._lock:
            self._index = index
            self._metadata = metadata
            self._id_map = id_map
            self._track_map = track_map
            self._next_id = next_id

    def _remove_by_faiss_id(self, fid: int) -> None:
        """Remove a single entry by FAISS ID. Caller must hold the lock."""
        meta = self._metadata.pop(fid, None)
        if meta is not None:
            self._id_map.pop(meta.embedding_id, None)
            self._track_map.pop(meta.local_track_id, None)

        ids_to_remove = np.array([fid], dtype=np.int64)
        self._index.remove_ids(ids_to_remove)
