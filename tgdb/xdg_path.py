from __future__ import annotations
import os
from pathlib import Path


class XDGPath:
    @staticmethod
    def config_home() -> Path:
        """Return $XDG_CONFIG_HOME or its default (~/.config)."""
        v = os.environ.get("XDG_CONFIG_HOME", "")
        if v:
            return Path(v)
        return Path.home() / ".config"

    @staticmethod
    def data_home() -> Path:
        """Return $XDG_DATA_HOME or its default (~/.local/share)."""
        v = os.environ.get("XDG_DATA_HOME", "")
        if v:
            return Path(v)
        return Path.home() / ".local" / "share"

    @staticmethod
    def cache_home() -> Path:
        """Return $XDG_CACHE_HOME or its default (~/.cache)."""
        v = os.environ.get("XDG_CACHE_HOME", "")
        if v:
            return Path(v)
        return Path.home() / ".cache"

    @staticmethod
    def state_home() -> Path:
        """Return $XDG_STATE_HOME or its default (~/.local/state)."""
        v = os.environ.get("XDG_STATE_HOME", "")
        if v:
            return Path(v)
        return Path.home() / ".local" / "state"
