"""Test helper functions for mtmc-service tests."""

from __future__ import annotations

import numpy as np


def make_l2_normalised(dim: int = 512, rng: np.random.Generator | None = None) -> np.ndarray:
    """Generate a random L2-normalised vector."""
    if rng is None:
        rng = np.random.default_rng()
    vec = rng.standard_normal(dim).astype(np.float32)
    vec /= np.linalg.norm(vec)
    return vec


def make_similar_vector(
    base: np.ndarray,
    similarity: float = 0.95,
    rng: np.random.Generator | None = None,
) -> np.ndarray:
    """Generate a vector with approximately the given cosine similarity to base."""
    if rng is None:
        rng = np.random.default_rng()
    noise = rng.standard_normal(len(base)).astype(np.float32)
    noise /= np.linalg.norm(noise)
    result = similarity * base + np.sqrt(1 - similarity**2) * noise
    result /= np.linalg.norm(result)
    return result
