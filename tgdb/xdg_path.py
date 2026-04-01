from __future__ import annotations
import os
from pathlib import Path


class XDGPath:
    @staticmethod
    def config_home() -> Path:
        """Return $XDG_CONFIG_HOME or its default (~/.config)."""
        v = os.environ.get("XDG_CONFIG_HOME", "")
        return Path(v) if v else Path.home() / ".config"

    @staticmethod
    def data_home() -> Path:
        """Return $XDG_DATA_HOME or its default (~/.local/share)."""
        v = os.environ.get("XDG_DATA_HOME", "")
        return Path(v) if v else Path.home() / ".local" / "share"

    @staticmethod
    def cache_home() -> Path:
        """Return $XDG_CACHE_HOME or its default (~/.cache)."""
        v = os.environ.get("XDG_CACHE_HOME", "")
        return Path(v) if v else Path.home() / ".cache"

    @staticmethod
    def state_home() -> Path:
        """Return $XDG_STATE_HOME or its default (~/.local/state)."""
        v = os.environ.get("XDG_STATE_HOME", "")
        return Path(v) if v else Path.home() / ".local" / "state"
