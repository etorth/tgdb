import os
from pathlib import Path


def _xdg_home(env_var: str, default_subpath: str) -> Path:
    """Return the XDG directory for *env_var*, falling back to *default_subpath* under $HOME.

    Per the XDG Base Directory Specification, an env var that is set but
    not absolute must be ignored.  Without this guard a user who exports
    e.g. ``XDG_CONFIG_HOME=relative/path`` ends up with config files
    silently looked up under whichever cwd tgdb happened to be launched
    from — a confusing failure mode.  Treat empty *and* relative values
    the same as unset.
    """
    value = os.environ.get(env_var, "")
    if value:
        path = Path(value)
        if path.is_absolute():
            return path
    return Path.home() / default_subpath


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
