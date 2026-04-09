"""Tests for the MTMC checkpoint snapshot/restore cycle.

Creates an index, adds embeddings, snapshots to a temp directory,
clears the index, restores from snapshot, and verifies all embeddings
are recovered with correct metadata.
"""

from __future__ import annotations

import tempfile
import time
from uuid import uuid4

import numpy as np
import pytest

from helpers import make_l2_normalised
from checkpoint import CheckpointData, CheckpointManager
from faiss_index import EmbeddingMeta, FAISSIndex


@pytest.fixture
def tmp_checkpoint_dir() -> str:
    with tempfile.TemporaryDirectory() as d:
        yield d


def test_local_snapshot_restore(
    faiss_index: FAISSIndex,
    rng: np.random.Generator,
    tmp_checkpoint_dir: str,
) -> None:
    """Snapshot to local disk, clear index, restore, verify all embeddings."""
    # Add 20 embeddings
    embeddings: list[tuple[str, np.ndarray, EmbeddingMeta]] = []
    for i in range(20):
        eid = str(uuid4())
        tid = str(uuid4())
        vec = make_l2_normalised(rng=rng)
        meta = EmbeddingMeta(
            embedding_id=eid,
            camera_id=f"cam-{i % 4}",
            local_track_id=tid,
            object_class="person",
            model_version="1.0.0",
            timestamp=time.time() - 60 + i,
        )
        faiss_index.add(eid, vec, meta)
        embeddings.append((eid, vec, meta))

    assert faiss_index.size() == 20

    # Snapshot
    mgr = CheckpointManager(
        local_path=tmp_checkpoint_dir,
        minio_client=None,
        minio_bucket="test",
        site_id="test",
        local_interval_s=0,
        minio_interval_s=0,
    )

    state = faiss_index.get_state()
    data = CheckpointData(
        index=state[0],
        metadata=state[1],
        id_map=state[2],
        track_map=state[3],
        next_id=state[4],
        embedding_count=faiss_index.size(),
    )
    size = mgr.save_local(data)
    assert size > 0
    assert mgr.local_file.exists()

    # Clear the index
    faiss_index.flush()
    assert faiss_index.size() == 0

    # Restore
    restored_data = mgr.load_from_local()
    assert restored_data is not None
    assert restored_data.embedding_count == 20

    faiss_index.restore_state(
        restored_data.index,
        restored_data.metadata,
        restored_data.id_map,
        restored_data.track_map,
        restored_data.next_id,
    )
    assert faiss_index.size() == 20

    # Verify each embedding is searchable and returns correct metadata
    for eid, vec, meta in embeddings:
        results = faiss_index.search(vec, k=1)
        assert len(results) >= 1
        top = results[0]
        assert top.meta.embedding_id == eid
        assert top.meta.camera_id == meta.camera_id
        assert top.meta.local_track_id == meta.local_track_id
        assert top.meta.object_class == meta.object_class
        assert top.meta.model_version == meta.model_version
        # Score should be ~1.0 for exact self-match
        assert top.score > 0.99


def test_restore_empty_when_no_checkpoint(tmp_checkpoint_dir: str) -> None:
    """Restore should return None when no checkpoint exists."""
    mgr = CheckpointManager(
        local_path=tmp_checkpoint_dir,
        minio_client=None,
        minio_bucket="test",
        site_id="test",
    )
    assert mgr.restore() is None


def test_checkpoint_preserves_after_removal(
    faiss_index: FAISSIndex,
    rng: np.random.Generator,
    tmp_checkpoint_dir: str,
) -> None:
    """Checkpoint after removing some embeddings should only contain remaining."""
    # Add 10 embeddings
    to_keep: list[tuple[str, np.ndarray, EmbeddingMeta]] = []
    to_remove: list[str] = []
    for i in range(10):
        eid = str(uuid4())
        tid = str(uuid4())
        vec = make_l2_normalised(rng=rng)
        meta = EmbeddingMeta(
            embedding_id=eid,
            camera_id=f"cam-{i % 3}",
            local_track_id=tid,
            object_class="car",
            model_version="1.0.0",
            timestamp=time.time(),
        )
        faiss_index.add(eid, vec, meta)
        if i % 2 == 0:
            to_remove.append(eid)
        else:
            to_keep.append((eid, vec, meta))

    assert faiss_index.size() == 10

    # Remove half
    for eid in to_remove:
        faiss_index.remove(eid)
    assert faiss_index.size() == 5

    # Snapshot
    mgr = CheckpointManager(
        local_path=tmp_checkpoint_dir,
        minio_client=None,
        minio_bucket="test",
        site_id="test",
        local_interval_s=0,
        minio_interval_s=0,
    )
    state = faiss_index.get_state()
    data = CheckpointData(
        index=state[0],
        metadata=state[1],
        id_map=state[2],
        track_map=state[3],
        next_id=state[4],
        embedding_count=faiss_index.size(),
    )
    mgr.save_local(data)

    # Clear and restore
    faiss_index.flush()
    restored = mgr.load_from_local()
    assert restored is not None
    faiss_index.restore_state(
        restored.index, restored.metadata, restored.id_map,
        restored.track_map, restored.next_id,
    )
    assert faiss_index.size() == 5

    # Verify only kept embeddings are present
    for eid, vec, meta in to_keep:
        results = faiss_index.search(vec, k=1)
        assert len(results) >= 1
        assert results[0].meta.embedding_id == eid


def test_checkpoint_metadata_fields(
    faiss_index: FAISSIndex,
    rng: np.random.Generator,
    tmp_checkpoint_dir: str,
) -> None:
    """Checkpoint metadata (version, timestamp, count) is populated."""
    vec = make_l2_normalised(rng=rng)
    meta = EmbeddingMeta(
        embedding_id=str(uuid4()),
        camera_id="cam-1",
        local_track_id=str(uuid4()),
        object_class="person",
        model_version="2.0.0",
        timestamp=time.time(),
    )
    faiss_index.add(meta.embedding_id, vec, meta)

    state = faiss_index.get_state()
    data = CheckpointData(
        index=state[0],
        metadata=state[1],
        id_map=state[2],
        track_map=state[3],
        next_id=state[4],
        model_version="2.0.0",
        embedding_count=faiss_index.size(),
    )

    assert data.checkpoint_version == 1
    assert data.timestamp > 0
    assert data.model_version == "2.0.0"
    assert data.embedding_count == 1


def test_tombstone_removal_then_checkpoint(
    faiss_index: FAISSIndex,
    rng: np.random.Generator,
    tmp_checkpoint_dir: str,
) -> None:
    """Tombstone (remove_by_track) should persist through checkpoint cycle."""
    tid = str(uuid4())
    vec = make_l2_normalised(rng=rng)
    meta = EmbeddingMeta(
        embedding_id=str(uuid4()),
        camera_id="cam-1",
        local_track_id=tid,
        object_class="person",
        model_version="1.0.0",
        timestamp=time.time(),
    )
    faiss_index.add(meta.embedding_id, vec, meta)
    assert faiss_index.size() == 1

    # Simulate tombstone
    faiss_index.remove_by_track(tid)
    assert faiss_index.size() == 0

    # Checkpoint empty index
    mgr = CheckpointManager(
        local_path=tmp_checkpoint_dir,
        minio_client=None,
        minio_bucket="test",
        site_id="test",
        local_interval_s=0,
        minio_interval_s=0,
    )
    state = faiss_index.get_state()
    data = CheckpointData(
        index=state[0],
        metadata=state[1],
        id_map=state[2],
        track_map=state[3],
        next_id=state[4],
        embedding_count=faiss_index.size(),
    )
    mgr.save_local(data)

    # Restore
    faiss_index.flush()
    restored = mgr.load_from_local()
    assert restored is not None
    faiss_index.restore_state(
        restored.index, restored.metadata, restored.id_map,
        restored.track_map, restored.next_id,
    )
    assert faiss_index.size() == 0
