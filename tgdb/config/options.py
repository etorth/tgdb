"""Option-oriented command handlers for the configuration package."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from .shared import _ALIASES, _apply_clipboard_path
from .types import _BOOL_OPTIONS, _INT_OPTIONS, _PATH_OPTIONS, _STR_OPTIONS

_log = logging.getLogger("tgdb.config")


class ConfigOptionMixin:
    """Mixin providing :set, :save, :history, and :highlight handlers."""

    async def _cmd_set(self, args: list[str]) -> str | None:
        if not args:
            return "set: missing argument"

        expr = args[0]
        if expr.startswith("no"):
            name = self._resolve_name(expr[2:])
            if name in _BOOL_OPTIONS:
                setattr(self.config, name, False)
                return None
        if "=" in expr:
            name, _, value = expr.partition("=")
            name = self._resolve_name(name.strip())
            if name == "history":
                return self._cmd_set_history_n(value.strip())
            return await self._set_option(name, value.strip())
        name = self._resolve_name(expr)
        if name in _BOOL_OPTIONS:
            setattr(self.config, name, True)
            return None
        return f"set: unknown option '{expr}'"


    def _cmd_set_history_n(self, val: str) -> str | None:
        try:
            n = int(val)
        except ValueError:
            return f"set history: invalid value {val!r} (must be a non-negative integer)"
        if n < 0:
            return "set history: value must be >= 0"

        bar = self._cmdline_bar
        if bar is not None:
            if n == 0:
                bar._history.clear()
            elif len(bar._history) > n:
                bar._history = bar._history[-n:]
        self.config.historysize = n
        return None


    async def _set_option(self, name: str, value: str) -> str | None:
        name = self._resolve_name(name)
        if name in _BOOL_OPTIONS:
            setattr(self.config, name, value.lower() not in ("0", "false", "off", "no"))
            _log.debug(f"set {name} = {getattr(self.config, name)!r}")
            return None
        if name in _INT_OPTIONS:
            try:
                setattr(self.config, name, int(value))
                if name == "timeoutlen":
                    self.km.timeout_ms = int(value)
                elif name == "ttimeoutlen":
                    self.km.ttimeout_ms = int(value)
                _log.debug(f"set {name} = {getattr(self.config, name)!r}")
            except ValueError:
                _log.warning(f"set: invalid integer value for {name}: {value!r}")
                return f"set: invalid integer '{value}'"
            return None
        if name in _STR_OPTIONS:
            setattr(self.config, name, value.lower())
            _log.debug(f"set {name} = {getattr(self.config, name)!r}")
            return None
        if name in _PATH_OPTIONS:
            setattr(self.config, name, value)
            _log.debug(f"set {name} = {value!r}")
            if value:
                _apply_clipboard_path(value)
            return None
        _log.warning(f"set: unknown option {name!r}")
        return f"set: unknown option '{name}'"


    def _resolve_name(self, name: str) -> str:
        return _ALIASES.get(name.lower(), name.lower())


    async def _cmd_save(self, args: list[str]) -> str | None:
        if not args or args[0].lower() != "history":
            if args:
                return f"save: unknown sub-command '{args[0]}'"
            return "save: unknown sub-command ''"
        bar = self._cmdline_bar
        if bar is None:
            return "save history: command-line bar not available"
        path = None
        if len(args) >= 2:
            path = Path(os.path.expanduser(args[1]))
        return bar.save_history(path, max_size=self.config.historysize)


    async def _cmd_highlight(self, args: list[str]) -> str | None:
        if not args:
            return "highlight: missing group name"
        group = args[0]
        fg = ""
        bg = ""
        attrs_val = ""
        for token in args[1:]:
            if "=" not in token:
                continue
            key, _, value = token.partition("=")
            key = key.lower()
            if key == "ctermfg":
                fg = value
            elif key == "ctermbg":
                bg = value
            elif key in ("cterm", "term"):
                attrs_val = value
        self.hl.set(group, fg=fg, bg=bg, attrs=attrs_val)
        return None


    async def _cmd_history(self) -> str | None:
        bar = self._cmdline_bar
        if bar is None:
            return "history: command-line bar not available"
        return bar.list_history()


    async def _cmd_history_run(self, cmd: str) -> str | None:
        bar = self._cmdline_bar
        if bar is None:
            return "history: command-line bar not available"
        history = bar._history
        if not history:
            return "history: no history entries"
        if cmd == "!!":
            for index in range(len(history) - 1, -1, -1):
                if not history[index].lstrip().startswith("#"):
                    return await self.execute_async(history[index])
            return "history: no commands to rerun"
        n_str = cmd[1:]
        try:
            n = int(n_str)
        except ValueError:
            return f"history: invalid index {n_str!r}"
        if n < 1 or n > len(history):
            return f"history: index {n} out of range (1–{len(history)})"
        return await self.execute_async(history[n - 1])


    async def _cmd_unmap(self, mode: str, args: list[str]) -> str | None:
        if not args:
            return "unmap: requires lhs"
        lhs = self._decode_keyseq_tokens(args[0])
        if not lhs:
            return "unmap: empty lhs"
        found = self.km.unmap(mode, lhs)
        if not found:
            return "unmap: no such mapping"
        return None
