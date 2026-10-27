"""Minimal pytest configuration for the extension boundary tests.

The guest suite under ``guest/`` runs inside the VM image, not on the host.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

collect_ignore = ["guest"]

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))


@pytest.fixture(scope="session")
def repo_root() -> Path:
    """The resolved repository root."""
    return Path(__file__).resolve().parents[2]
