"""Shared fixtures for bulk-collector unit tests."""

from __future__ import annotations

import sys
from pathlib import Path

_service_root = str(Path(__file__).resolve().parent.parent)
if _service_root not in sys.path:
    sys.path.insert(0, _service_root)

