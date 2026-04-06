"""Shared fixtures for edge-agent unit tests."""

from __future__ import annotations

import sys
from pathlib import Path

# Add the service root to the Python path so plain imports work.
_service_root = str(Path(__file__).resolve().parent.parent)
if _service_root not in sys.path:
    sys.path.insert(0, _service_root)
