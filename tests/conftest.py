"""Shared pytest fixtures."""

import pytest


@pytest.fixture(autouse=True)
def isolated_xdg(tmp_path, monkeypatch):
    """Point every XDG directory at a temp dir.

    Keeps tests from reading the user's tgdbrc or writing to their command
    history (tgdb saves history on exit, including in ``--batch`` mode).
    """
    for var in ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME"):
        path = tmp_path / "xdg" / var.lower()
        path.mkdir(parents=True)
        monkeypatch.setenv(var, str(path))
