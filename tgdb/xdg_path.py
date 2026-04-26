import os
from pathlib import Path


def _xdg_home(env_var: str, default_subpath: str) -> Path:
    """Return the XDG directory for *env_var*, falling back to *default_subpath* under $HOME."""
    value = os.environ.get(env_var, "")
    return Path(value) if value else Path.home() / default_subpath


class XDGPath:
    @staticmethod
    def config_home() -> Path:
        """Return $XDG_CONFIG_HOME or its default (~/.config)."""
        return _xdg_home("XDG_CONFIG_HOME", ".config")


    @staticmethod
    def data_home() -> Path:
        """Return $XDG_DATA_HOME or its default (~/.local/share)."""
        return _xdg_home("XDG_DATA_HOME", ".local/share")


    @staticmethod
    def cache_home() -> Path:
        """Return $XDG_CACHE_HOME or its default (~/.cache)."""
        return _xdg_home("XDG_CACHE_HOME", ".cache")


    @staticmethod
    def state_home() -> Path:
        """Return $XDG_STATE_HOME or its default (~/.local/state)."""
        return _xdg_home("XDG_STATE_HOME", ".local/state")
