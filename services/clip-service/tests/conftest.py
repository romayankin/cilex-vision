"""Shared test fixtures for clip-service tests."""

from __future__ import annotations

import sys
from pathlib import Path

# Add the service root to sys.path for plain imports.
SERVICE_ROOT = Path(__file__).resolve().parent.parent
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))
