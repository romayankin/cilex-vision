"""CPU stub embedder that returns a zero 512-d vector.

Re-ID embeddings are meaningless without a real model, but the downstream
pipeline (publisher, MTMC consumer) expects a 512-d L2-compatible vector.
A zero vector keeps schemas valid while producing zero-similarity matches,
which effectively disables MTMC association in CPU-only deployments.
"""

from __future__ import annotations

import logging

import numpy as np

logger = logging.getLogger(__name__)


class CpuEmbedderClient:
    EMBED_DIM = 512

    def __init__(self) -> None:
        self._warned = False

    async def extract(
        self,
        frame: np.ndarray,
        bbox: tuple[float, float, float, float],
    ) -> np.ndarray:
        if not self._warned:
            logger.warning(
                "CPU embedder stub in use — Re-ID embeddings are zero vectors"
            )
            self._warned = True
        return np.zeros(self.EMBED_DIM, dtype=np.float32)
