"""
Configuration parser for tgdb.

Supports $XDG_CONFIG_HOME/tgdb/tgdbrc (default: ~/.config/tgdb/tgdbrc)
as the initialization file.
Supports all :set options, :highlight, :map, :imap, :unmap, :iunmap.
"""

from __future__ import annotations

import builtins
from typing import Callable, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .highlight_groups import HighlightGroups
    from .key_mapper import KeyMapper

from .config_execution import ConfigExecutionMixin
from .config_keys import ConfigKeyMixin
from .config_options import ConfigOptionMixin
from .config_types import (  # noqa: F401 — re-exported
    Config,
    UserCommandDef,
)
from .config_commands import UserCommandMixin
from .config_python import PythonExecMixin

class ConfigParser(
    ConfigExecutionMixin,
    ConfigOptionMixin,
    ConfigKeyMixin,
    UserCommandMixin,
    PythonExecMixin,
):
    """Parse tgdb/cgdb-style config commands and update live runtime objects.

    Public interface
    ----------------
    ``ConfigParser(config, highlight_groups, key_mapper)``
        Create the parser around the live objects it mutates.

    ``set_cmdline_bar(bar)``
        Inject the command-line bar so history-oriented commands can delegate to
        the UI object that owns the history buffer.

    ``register_handler(name, fn)``
        Register an app-side command handler for commands that the config layer
        recognizes but the application ultimately executes.

    ``set_py_globals(mapping)``
        Extend the persistent ``:python`` / ``:pyfile`` namespace with live
        objects such as the app instance.

    ``default_rc_path()``, ``load_file_async(path)``, ``execute_async(line)``
        Resolve, load, and execute config/status-bar commands.

    Callers should treat ``ConfigParser`` as the black-box command dispatcher
    for rc files and ``:`` commands. The parser owns tokenization, aliases,
    built-in option handling, user-defined commands, and Python execution.
    """

    def __init__(
        self,
        config: Config,
        highlight_groups: "HighlightGroups",
        key_mapper: "KeyMapper",
    ) -> None:
        self.config = config
        self.hl = highlight_groups
        self.km = key_mapper
        self._handlers: dict[str, Callable[[list[str]], Optional[str]]] = {}
        self._py_namespace: dict = {
            "__builtins__": builtins,
            "config": self.config,
            "hl": self.hl,
            "km": self.km,
        }
        self._user_commands: dict[str, UserCommandDef] = {}
        self._exec_depth: int = 0
        self._cmdline_bar = None


    def set_cmdline_bar(self, bar) -> None:
        """Inject a reference to the CommandLineBar for history operations."""
        self._cmdline_bar = bar


    def register_handler(self, name: str, fn: Callable[[list[str]], Optional[str]]) -> None:
        """Register an extra command handler (e.g., GDB debug commands)."""
        self._handlers[name] = fn


    def set_py_globals(self, d: dict) -> None:
        """Merge *d* into the persistent Python namespace used by :python/:pyfile.

        Call this after constructing ConfigParser to inject live objects (e.g.
        the TGDBApp instance as ``app``) that scripts can reference.
        """
        self._py_namespace.update(d)
