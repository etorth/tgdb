"""Option-oriented command handlers for the configuration package."""

import logging
import os
from pathlib import Path

import pyperclip

from .types import _BOOL_OPTIONS, _INT_OPTIONS, _PATH_OPTIONS, _STR_OPTIONS

_log = logging.getLogger("tgdb.config")


_ALIASES: dict[str, str] = {
    "asr": "autosourcereload",
    "arrowstyle": "executinglinedisplay",
    "as": "executinglinedisplay",
    "dwc": "debugwincolor",
    "dis": "disasm",
    "ecl": "expandchildlimit",
    "eld": "executinglinedisplay",
    "hls": "hlsearch",
    "ic": "ignorecase",
    "sbbs": "scrollbackbuffersize",
    "sld": "selectedlinedisplay",
    "sdc": "showdebugcommands",
    "syn": "syntax",
    "to": "timeout",
    "tm": "timeoutlen",
    "ttm": "ttimeoutlen",
    "ts": "tabstop",
    "wmh": "winminheight",
    "wmw": "winminwidth",
    "wso": "winsplitorientation",
    "ws": "wrapscan",
}


def _apply_clipboard_path(path: str) -> None:
    """Apply a clipboardpath setting immediately."""
    dirname = os.path.dirname(path)
    basename = os.path.basename(path)
    if dirname:
        current = os.environ.get("PATH", "")
        parts = current.split(os.pathsep)
        if dirname not in parts:
            os.environ["PATH"] = dirname + os.pathsep + current
    if basename:
        try:
            pyperclip.set_clipboard(basename)
        except Exception:
            pass


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
        self._apply_historysize(n)
        return None


    def _apply_historysize(self, n: int) -> None:
        """Set ``config.historysize`` *and* prune the live cmdline-bar buffer.

        Both ``set history=N`` (the cgdb short form, routed through
        ``_cmd_set_history_n``) and ``set historysize=N`` (the canonical
        long form, routed through ``_set_option``'s ``_INT_OPTIONS``
        branch) need to enforce the new limit on the in-memory history
        immediately — otherwise the live buffer keeps its previous length
        until the next restart even though ``config.historysize`` reads
        as the new value.  Centralised here so both forms behave the same.
        """
        bar = self._cmdline_bar
        if bar is not None:
            if n == 0:
                bar._history.clear()
            elif len(bar._history) > n:
                bar._history = bar._history[-n:]
        self.config.historysize = n


    async def _set_option(self, name: str, value: str) -> str | None:
        name = self._resolve_name(name)
        if name in _BOOL_OPTIONS:
            setattr(self.config, name, value.lower() not in ("0", "false", "off", "no"))
            _log.debug(f"set {name} = {getattr(self.config, name)!r}")
            return None
        if name in _INT_OPTIONS:
            try:
                n = int(value)
            except ValueError:
                _log.warning(f"set: invalid integer value for {name}: {value!r}")
                return f"set: invalid integer '{value}'"
            if name == "historysize":
                # Route through the same helper as ``set history=N`` so
                # the in-memory cmdline buffer is pruned to the new limit
                # immediately rather than at next restart.
                self._apply_historysize(n)
            else:
                setattr(self.config, name, n)
            if name == "timeoutlen":
                self.km.timeout_ms = n
            elif name == "ttimeoutlen":
                self.km.ttimeout_ms = n
            _log.debug(f"set {name} = {getattr(self.config, name)!r}")
            return None
        if name in _STR_OPTIONS:
            setattr(self.config, name, value.lower())
            _log.debug(f"set {name} = {getattr(self.config, name)!r}")
            return None
        if name in _PATH_OPTIONS:
            if name == "memoryformatter":
                err = self._apply_memoryformatter(value)
                if err is not None:
                    return err
                return None
            setattr(self.config, name, value)
            _log.debug(f"set {name} = {value!r}")
            if value:
                _apply_clipboard_path(value)
            return None
        _log.warning(f"set: unknown option {name!r}")
        return f"set: unknown option '{name}'"


    def _resolve_name(self, name: str) -> str:
        return _ALIASES.get(name.lower(), name.lower())


    def _apply_memoryformatter(self, value: str) -> str | None:
        """Evaluate *value* and install the resulting memory formatter.

        Empty string resets to the default ``MemoryFormatter()``.
        Returns an error string on failure; on success returns ``None``
        and broadcasts the new formatter to all subscribers.
        """
        from ..memory_pane import build_formatter, MemoryFormatter

        if not value.strip():
            obj = MemoryFormatter()
            err = None
        else:
            obj, err = build_formatter(value, self._py_namespace)
        if err is not None or obj is None:
            _log.warning(f"set memoryformatter: {err}")
            self.config._memoryformatter_obj = MemoryFormatter()
            self.config.notify_memoryformatter_changed()
            return f"set memoryformatter: {err} (falling back to default)"
        self.config.memoryformatter = value
        self.config._memoryformatter_obj = obj
        self.config.notify_memoryformatter_changed()
        _log.debug(f"set memoryformatter = {value!r}")
        return None


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
        attrs_parts: list[str] = []
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
                # Merge attribute tokens instead of last-token-wins so
                # ``:highlight Foo cterm=bold term=italic`` ends up with
                # ``bold,italic`` applied.  Each value may itself be a
                # comma-separated list (``cterm=bold,underline``); join
                # them all into a single comma-separated string for the
                # downstream ``set()`` parser to split.
                if value:
                    attrs_parts.append(value)
        attrs_val = ",".join(attrs_parts)
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
