"""Checkpoint manager for FAISS index snapshot/restore.

Periodically serialises the FAISS index and metadata to local disk and
uploads to MinIO for fault tolerance.  On startup, restores from the
most recent checkpoint (MinIO first, local fallback, empty as last resort).
"""

from __future__ import annotations

import io
import logging
import pickle
import time
from pathlib import Path
from typing import Any, Optional

from metrics import CHECKPOINT_LAG_SECONDS, CHECKPOINT_SIZE_BYTES

logger = logging.getLogger(__name__)

CHECKPOINT_VERSION = 1


class CheckpointData:
    """Serialisable snapshot of the FAISS index state."""

    __slots__ = (
        "checkpoint_version",
        "timestamp",
        "model_version",
        "embedding_count",
        "index",
        "metadata",
        "id_map",
        "track_map",
        "next_id",
    )

    def __init__(
        self,
        index: Any,
        metadata: dict[int, Any],
        id_map: dict[str, int],
        track_map: dict[str, int],
        next_id: int,
        model_version: str = "",
        embedding_count: int = 0,
    ) -> None:
        self.checkpoint_version = CHECKPOINT_VERSION
        self.timestamp = time.time()
        self.model_version = model_version
        self.embedding_count = embedding_count
        self.index = index
        self.metadata = metadata
        self.id_map = id_map
        self.track_map = track_map
        self.next_id = next_id


class CheckpointManager:
    """Manages periodic local and remote (MinIO) checkpoints."""

    def __init__(
        self,
        local_path: str,
        minio_client: Any,
        minio_bucket: str,
        site_id: str,
        local_interval_s: int = 60,
        minio_interval_s: int = 300,
    ) -> None:
        self._local_path = Path(local_path)
        self._minio = minio_client
        self._minio_bucket = minio_bucket
        self._site_id = site_id
        self._local_interval_s = local_interval_s
        self._minio_interval_s = minio_interval_s

        self._last_local_save = 0.0
        self._last_minio_save = 0.0

        self._local_path.mkdir(parents=True, exist_ok=True)

    @property
    def local_file(self) -> Path:
        return self._local_path / "latest.pkl"

    @property
    def minio_key(self) -> str:
        return f"{self._site_id}/latest.pkl"

    def should_save_local(self) -> bool:
        return (time.time() - self._last_local_save) >= self._local_interval_s

    def should_save_minio(self) -> bool:
        return (time.time() - self._last_minio_save) >= self._minio_interval_s

    def save_local(self, data: CheckpointData) -> int:
        """Serialise checkpoint to local disk. Returns size in bytes."""
        payload = pickle.dumps(data, protocol=pickle.HIGHEST_PROTOCOL)
        tmp = self.local_file.with_suffix(".tmp")
        tmp.write_bytes(payload)
        tmp.rename(self.local_file)

        size = len(payload)
        self._last_local_save = time.time()
        CHECKPOINT_SIZE_BYTES.set(size)
        CHECKPOINT_LAG_SECONDS.set(0)
        logger.info(
            "Local checkpoint saved: %d embeddings, %.1f KB",
            data.embedding_count,
            size / 1024,
        )
        return size

    def save_minio(self, data: CheckpointData) -> None:
        """Upload checkpoint to MinIO."""
        if self._minio is None:
            return

        payload = pickle.dumps(data, protocol=pickle.HIGHEST_PROTOCOL)
        buf = io.BytesIO(payload)

        try:
            self._ensure_bucket()
            self._minio.put_object(
                self._minio_bucket,
                self.minio_key,
                buf,
                length=len(payload),
                content_type="application/octet-stream",
            )
            self._last_minio_save = time.time()
            logger.info(
                "MinIO checkpoint uploaded: %s/%s (%d bytes)",
                self._minio_bucket,
                self.minio_key,
                len(payload),
            )
        except Exception:
            logger.warning("Failed to upload checkpoint to MinIO", exc_info=True)

    def load_from_minio(self) -> Optional[CheckpointData]:
        """Try to load checkpoint from MinIO. Returns None on failure."""
        if self._minio is None:
            return None
        try:
            response = self._minio.get_object(self._minio_bucket, self.minio_key)
            payload = response.read()
            response.close()
            response.release_conn()
            data = pickle.loads(payload)  # noqa: S301
            logger.info(
                "Restored checkpoint from MinIO: %d embeddings",
                data.embedding_count,
            )
            return data
        except Exception:
            logger.info("No checkpoint available in MinIO")
            return None

    def load_from_local(self) -> Optional[CheckpointData]:
        """Try to load checkpoint from local disk. Returns None on failure."""
        if not self.local_file.exists():
            return None
        try:
            payload = self.local_file.read_bytes()
            data = pickle.loads(payload)  # noqa: S301
            logger.info(
                "Restored checkpoint from local: %d embeddings",
                data.embedding_count,
            )
            return data
        except Exception:
            logger.warning("Failed to load local checkpoint", exc_info=True)
            return None

    def restore(self) -> Optional[CheckpointData]:
        """Try MinIO first, then local, return None for empty start."""
        data = self.load_from_minio()
        if data is not None:
            return data
        data = self.load_from_local()
        if data is not None:
            return data
        logger.info("No checkpoint found — starting with empty index")
        return None

    def update_lag_metric(self) -> None:
        """Update the checkpoint lag gauge."""
        last = max(self._last_local_save, self._last_minio_save)
        if last > 0:
            CHECKPOINT_LAG_SECONDS.set(time.time() - last)

    def _ensure_bucket(self) -> None:
        if self._minio is None:
            return
        if not self._minio.bucket_exists(self._minio_bucket):
            self._minio.make_bucket(self._minio_bucket)
