"""Shared pytest fixtures."""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def tmp_db(tmp_path: Path) -> Path:
    """A fresh SQLite database path under a temp dir."""
    return tmp_path / "state.db"


@pytest.fixture
def tmp_orch_dir(tmp_path: Path) -> Path:
    """A fresh `.orchestra/` directory under a temp dir."""
    d = tmp_path / ".orchestra"
    d.mkdir()
    return d
