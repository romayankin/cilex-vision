"""Shared test fixtures for mtmc-service tests."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

# Add service root to sys.path for plain imports.
SERVICE_ROOT = Path(__file__).resolve().parent.parent
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

# Add tests dir for helpers import
TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

from faiss_index import FAISSIndex  # noqa: E402


@pytest.fixture
def faiss_index() -> FAISSIndex:
    """Fresh FAISS index for testing."""
    return FAISSIndex(dimension=512, active_horizon_minutes=30)


@pytest.fixture
def rng() -> np.random.Generator:
    """Deterministic RNG for reproducible tests."""
    return np.random.default_rng(42)
