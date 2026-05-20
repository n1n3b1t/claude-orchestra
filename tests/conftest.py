"""Shared pytest fixtures."""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True, scope="session")
def _force_hook_fallback() -> None:
    """Force the in-process hook path for the suite.

    The v2.1 daemon path (orchestra/hookd.py) is exercised explicitly by
    tests/test_hookd.py, which monkeypatches this env away. Every other
    test must continue to hit the in-process dispatch so the suite stays
    deterministic and offline.
    """
    import os
    os.environ["ORCHESTRA_FORCE_HOOK_FALLBACK"] = "1"


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
