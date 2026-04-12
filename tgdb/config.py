"""
Configuration parser for tgdb.

Supports $XDG_CONFIG_HOME/tgdb/tgdbrc (default: ~/.config/tgdb/tgdbrc)
as the initialization file.
Supports all :set options, :highlight, :map, :imap, :unmap, :iunmap.
"""

from __future__ import annotations

import builtins
import logging
import os
import re
import shlex
from pathlib import Path
from typing import Callable, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .highlight_groups import HighlightGroups
    from .key_mapper import KeyMapper

from .xdg_path import XDGPath
from .config_types import (  # noqa: F401 — re-exported
    Config,
    UserCommandDef,
    _BOOL_OPTIONS,
    _INT_OPTIONS,
    _STR_OPTIONS,
    _PATH_OPTIONS,
    _VALID_NARGS,
    _TGDB_RESERVED_PREFIX,
    _CMD_NAME_RE,
)
from .config_commands import UserCommandMixin
from .config_python import PythonExecMixin

_log = logging.getLogger("tgdb.config")


# Abbreviation → canonical name
_ALIASES: dict[str, str] = {
    "asr": "autosourcereload",
    "arrowstyle": "executinglinedisplay",  # deprecated alias (cgdb cgdbrc.cpp)
    "as": "executinglinedisplay",  # short form of arrowstyle
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
    """Apply a clipboardpath setting immediately.

    1. Prepends dirname(path) to $PATH so the tool is findable.
    2. Calls pyperclip.set_clipboard(basename(path)) to select the backend.

    Both steps are idempotent: the directory is only prepended once, and
    set_clipboard replaces the previous selection each time.
    """
    dirname = os.path.dirname(path)
    basename = os.path.basename(path)
    if dirname:
        current = os.environ.get("PATH", "")
        parts = current.split(os.pathsep)
        if dirname not in parts:
            os.environ["PATH"] = dirname + os.pathsep + current
    if basename:
        try:
            import pyperclip

            pyperclip.set_clipboard(basename)
        except Exception:
            pass


class ConfigParser(UserCommandMixin, PythonExecMixin):
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

    def register_handler(
        self, name: str, fn: Callable[[list[str]], Optional[str]]
    ) -> None:
        """Register an extra command handler (e.g., GDB debug commands)."""
        self._handlers[name] = fn

    def set_py_globals(self, d: dict) -> None:
        """Merge *d* into the persistent Python namespace used by :python/:pyfile.

        Call this after constructing ConfigParser to inject live objects (e.g.
        the TGDBApp instance as ``app``) that scripts can reference.
        """
        self._py_namespace.update(d)

    # ------------------------------------------------------------------
    # File loading
    # ------------------------------------------------------------------

    def default_rc_path(self) -> Optional[Path]:
        """Return the default rc file path (~/.config/tgdb/tgdbrc), or None if it doesn't exist."""
        path = XDGPath.config_home() / "tgdb" / "tgdbrc"
        if path.exists():
            return path
        return None

    async def load_file_async(
        self, path: str, print_fn: Optional[Callable] = None
    ) -> Optional[str]:
        """Load and execute every non-blank line in *path* asynchronously.

        Supports heredoc blocks (``python << MARKER`` … ``MARKER``) so that
        multi-line Python function definitions work just like typing them
        manually in the status bar.  Each line is routed through
        execute_async() so sourced files can use top-level ``await``.

        Returns an error string if the file cannot be opened, None on success.
        Errors from individual lines are silently ignored.
        """
        try:
            with open(path) as f:
                raw_lines = f.readlines()
        except OSError as e:
            return f"source: cannot open '{path}': {e}"

        _log.info(f"loading rc file: {path}")
        i = 0
        while i < len(raw_lines):
            line = raw_lines[i].rstrip("\n")
            stripped = line.strip()
            i += 1

            if not stripped:
                continue

            # Detect heredoc: [:]python << MARKER
            check = stripped.lstrip(":")
            m = re.match(r"^(python|py)\s+<<\s+(\S+)\s*$", check, re.IGNORECASE)
            if m:
                cmd_name = m.group(1).lower()
                marker = m.group(2)
                code_lines: list[str] = []
                while i < len(raw_lines):
                    code_line = raw_lines[i].rstrip("\n")
                    i += 1
                    if code_line.strip() == marker:
                        break
                    code_lines.append(code_line)
                await self.execute_async(
                    f"{cmd_name}\n" + "\n".join(code_lines),
                    print_fn=print_fn,
                )
                continue

            await self.execute_async(stripped, print_fn=print_fn)

        return None

    # ------------------------------------------------------------------
    # Command execution — single async entry point
    # ------------------------------------------------------------------

    async def execute_async(
        self, line: str, print_fn: Optional[Callable] = None
    ) -> Optional[str]:
        """Execute one config/status-bar command asynchronously.

        This is the sole command-execution entry point.  All commands —
        :set, :highlight, :map, :source, :python, user commands, etc. —
        are dispatched from here.

        Lines starting with optional whitespace then '#' are treated as
        comments and are a no-op (the caller may still record them in
        history).
        """
        line = line.strip()
        if not line:
            return None
        if line.startswith(":"):
            line = line[1:].strip()
        if not line or line.startswith("#"):
            return None

        # :python / :py / :pyfile / :pyf take a raw (un-tokenised) argument.
        py_match = re.match(
            r"^(python|pyfile|pyf|py)\s*(.*)", line, re.DOTALL | re.IGNORECASE
        )
        if py_match:
            return await self._dispatch_python_command(py_match, print_fn)

        # :command needs raw handling to preserve the replacement template.
        cmd_m = re.match(r"^command\b\s*(.*)", line, re.DOTALL | re.IGNORECASE)
        if cmd_m:
            return await self._cmd_command(cmd_m.group(1))

        # :evaluate / :signal take an arbitrary expression.
        eval_m = re.match(
            r"^(evaluate|signal)\b\s*(.*)", line, re.DOTALL | re.IGNORECASE
        )
        if eval_m:
            eval_cmd, eval_expr = eval_m.group(1).lower(), eval_m.group(2)
            handler = self._handlers.get(eval_cmd)
            if handler is not None:
                return handler([eval_expr] if eval_expr else [])
            return None

        # :map / :imap need raw handling so spaces in the RHS are preserved.
        map_m = re.match(
            r"^(imap|im|map)\b\s*(\S+)\s*(.*)", line, re.DOTALL | re.IGNORECASE
        )
        if map_m:
            return self._dispatch_map_command(map_m)

        try:
            parts = shlex.split(line)
        except ValueError:
            parts = line.split()
        if not parts:
            return None

        raw_cmd = parts[0]
        cmd = raw_cmd.lower()
        args = parts[1:]

        # Pure number: :12 → goto line; :+5 → scroll down; :-3 → scroll up
        if re.match(r"^[+-]?\d+$", raw_cmd):
            handler = self._handlers.get("_goto_line")
            if handler is not None:
                return handler([raw_cmd])
            return None

        # History replay: !! → last entry; !N → entry N
        if raw_cmd == "!!" or (raw_cmd.startswith("!") and raw_cmd[1:].isdigit()):
            return await self._cmd_history_run(raw_cmd)

        # Registered (app-side) handlers — sync callables, no await needed
        if cmd in self._handlers:
            return self._handlers[cmd](args)

        return await self._dispatch_builtin_command(cmd, raw_cmd, args, print_fn, line)

    async def _dispatch_python_command(
        self, match: re.Match, print_fn: Optional[Callable]
    ) -> Optional[str]:
        """Dispatch :python/:py/:pyfile/:pyf commands."""
        cmd, raw_arg = match.group(1).lower(), match.group(2)
        if cmd in ("python", "py"):
            # Handle heredoc format: "<< MARKER\n...\nMARKER"
            heredoc_m = re.match(r"^<<\s*(\S+)\s*\n(.*)", raw_arg, re.DOTALL)
            if heredoc_m:
                marker = heredoc_m.group(1)
                body_lines = heredoc_m.group(2).split("\n")
                code_lines = []
                for bl in body_lines:
                    if bl.strip() == marker:
                        break
                    code_lines.append(bl)
                raw_arg = "\n".join(code_lines)
            return await self._exec_py_async(raw_arg, "<tgdb:python>", print_fn)
        else:
            return await self._exec_pyfile_async(raw_arg.strip(), print_fn)

    def _dispatch_map_command(self, match: re.Match) -> Optional[str]:
        """Dispatch :map / :imap / :im commands."""
        mcmd = match.group(1).lower()
        mode = "gdb" if mcmd in ("imap", "im") else "tgdb"
        lhs = self._decode_keyseq_tokens(match.group(2))
        rhs = self._decode_keyseq_tokens(match.group(3))
        if not lhs:
            return "map: empty lhs"
        _log.debug(f"map {lhs!r} -> {rhs!r}")
        self.km.map(mode, lhs, rhs)
        return None

    async def _dispatch_builtin_command(
        self,
        cmd: str,
        raw_cmd: str,
        args: list[str],
        print_fn: Optional[Callable],
        raw_line: str = "",
    ) -> Optional[str]:
        """Dispatch built-in :set / :highlight / :unmap / :source etc."""
        if cmd == "set":
            return await self._cmd_set(args)
        if cmd in ("highlight", "hi"):
            return await self._cmd_highlight(args)
        if cmd in ("unmap", "unm"):
            return await self._cmd_unmap("tgdb", args)
        if cmd in ("iunmap", "iu"):
            return await self._cmd_unmap("gdb", args)
        if cmd == "noh":
            self.config.hlsearch = False
            return None
        if cmd == "syntax":
            if args:
                return await self._set_option("syntax", args[0])
            return None
        if cmd in ("source", "so"):
            if not args:
                return "source: missing filename"
            return await self.load_file_async(
                os.path.expanduser(args[0]),
                print_fn=print_fn,
            )
        if cmd == "save":
            return await self._cmd_save(args)
        if cmd == "history":
            return await self._cmd_history()

        # User-defined commands (must start with an uppercase letter)
        if raw_cmd[:1].isupper():
            ucmd, amb_err = self._lookup_user_command(raw_cmd)
            if amb_err:
                return amb_err
            if ucmd is not None:
                m = re.match(r"\S+\s*(.*)", raw_line, re.DOTALL)
                raw_args = m.group(1) if m else ""
                return await self._exec_user_command_async(
                    ucmd,
                    raw_args,
                    print_fn=print_fn,
                )

        return f"Unknown command: {cmd}"

    # ------------------------------------------------------------------
    # :command — user-defined commands
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # :set
    # ------------------------------------------------------------------

    async def _cmd_set(self, args: list[str]) -> Optional[str]:
        if not args:
            return "set: missing argument"

        expr = args[0]
        # Boolean negation: noXXX
        if expr.startswith("no"):
            name = self._resolve_name(expr[2:])
            if name in _BOOL_OPTIONS:
                setattr(self.config, name, False)
                return None
        # Assignment: name=value
        if "=" in expr:
            name, _, value = expr.partition("=")
            name = self._resolve_name(name.strip())
            # Special case: history=N sets buffer size
            if name == "history":
                return self._cmd_set_history_n(value.strip())
            return await self._set_option(name, value.strip())
        # Boolean enable
        name = self._resolve_name(expr)
        if name in _BOOL_OPTIONS:
            setattr(self.config, name, True)
            return None
        return f"set: unknown option '{expr}'"

    def _cmd_set_history_n(self, val: str) -> Optional[str]:
        """Handle: set history=N  (N >= 0; 0 = disable history buffer)."""
        try:
            n = int(val)
        except ValueError:
            return (
                f"set history: invalid value {val!r} (must be a non-negative integer)"
            )
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

    async def _set_option(self, name: str, value: str) -> Optional[str]:
        name = self._resolve_name(name)
        if name in _BOOL_OPTIONS:
            setattr(self.config, name, value.lower() not in ("0", "false", "off", "no"))
            _log.debug(f"set {name} = {getattr(self.config, name)!r}")
            return None
        elif name in _INT_OPTIONS:
            try:
                setattr(self.config, name, int(value))
                # Propagate timeout values to key mapper
                if name == "timeoutlen":
                    self.km.timeout_ms = int(value)
                elif name == "ttimeoutlen":
                    self.km.ttimeout_ms = int(value)
                _log.debug(f"set {name} = {getattr(self.config, name)!r}")
            except ValueError:
                _log.warning(f"set: invalid integer value for {name}: {value!r}")
                return f"set: invalid integer '{value}'"
            return None
        elif name in _STR_OPTIONS:
            setattr(self.config, name, value.lower())
            _log.debug(f"set {name} = {getattr(self.config, name)!r}")
            return None
        elif name in _PATH_OPTIONS:
            setattr(self.config, name, value)  # preserve case
            _log.debug(f"set {name} = {value!r}")
            if value:
                _apply_clipboard_path(value)
            return None
        _log.warning(f"set: unknown option {name!r}")
        return f"set: unknown option '{name}'"

    def _resolve_name(self, name: str) -> str:
        return _ALIASES.get(name.lower(), name.lower())

    # ------------------------------------------------------------------
    # :save
    # ------------------------------------------------------------------

    async def _cmd_save(self, args: list[str]) -> Optional[str]:
        """Handle: save history [filename]"""
        if not args or args[0].lower() != "history":
            if args:
                return f"save: unknown sub-command '{args[0]}'"
            else:
                return "save: unknown sub-command ''"
        bar = self._cmdline_bar
        if bar is None:
            return "save history: command-line bar not available"
        if len(args) >= 2:
            path = Path(os.path.expanduser(args[1]))
        else:
            path = None
        max_size = self.config.historysize
        return bar.save_history(path, max_size=max_size)

    # ------------------------------------------------------------------
    # :highlight / :hi
    # ------------------------------------------------------------------

    async def _cmd_highlight(self, args: list[str]) -> Optional[str]:
        if not args:
            return "highlight: missing group name"
        group = args[0]
        fg = bg = attrs_val = ""
        for tok in args[1:]:
            if "=" in tok:
                k, _, v = tok.partition("=")
                k = k.lower()
                if k == "ctermfg":
                    fg = v
                elif k == "ctermbg":
                    bg = v
                elif k in ("cterm", "term"):
                    attrs_val = v
        self.hl.set(group, fg=fg, bg=bg, attrs=attrs_val)
        return None

    # ------------------------------------------------------------------
    # :history  /  !!  /  !N
    # ------------------------------------------------------------------

    async def _cmd_history(self) -> Optional[str]:
        """Handle: :history — list all recorded commands with line numbers."""
        bar = self._cmdline_bar
        if bar is None:
            return "history: command-line bar not available"
        return bar.list_history()

    async def _cmd_history_run(self, cmd: str) -> Optional[str]:
        """Handle: !! (rerun last non-comment) or !N (rerun entry N)."""
        bar = self._cmdline_bar
        if bar is None:
            return "history: command-line bar not available"
        history = bar._history
        if not history:
            return "history: no history entries"
        if cmd == "!!":
            # Find last non-comment entry
            for i in range(len(history) - 1, -1, -1):
                if not history[i].lstrip().startswith("#"):
                    return await self.execute_async(history[i])
            return "history: no commands to rerun"
        # !N
        n_str = cmd[1:]
        try:
            n = int(n_str)
        except ValueError:
            return f"history: invalid index {n_str!r}"
        if n < 1 or n > len(history):
            return f"history: index {n} out of range (1\u2013{len(history)})"
        return await self.execute_async(history[n - 1])

    # ------------------------------------------------------------------
    # :map / :imap
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # :unmap / :iunmap
    # ------------------------------------------------------------------

    async def _cmd_unmap(self, mode: str, args: list[str]) -> Optional[str]:
        if not args:
            return "unmap: requires lhs"
        lhs = self._decode_keyseq_tokens(args[0])
        if not lhs:
            return "unmap: empty lhs"
        found = self.km.unmap(mode, lhs)
        if not found:
            return "unmap: no such mapping"
        return None

    # ------------------------------------------------------------------
    # Key sequence decoding — cgdb notation → Textual key-name token list
    # ------------------------------------------------------------------

    # Maps lower-cased cgdb <Name> identifiers to Textual event.key strings
    _KEY_TOKENS: dict[str, str] = {
        "space": "space",
        "enter": "enter",
        "return": "enter",
        "cr": "enter",
        "nl": "ctrl+j",
        "tab": "tab",
        "esc": "escape",
        "escape": "escape",
        "bs": "backspace",
        "backspace": "backspace",
        "del": "delete",
        "delete": "delete",
        "insert": "insert",
        "nul": "ctrl+@",
        "lt": "<",
        "bslash": "\\",
        "bar": "|",
        "up": "up",
        "down": "down",
        "left": "left",
        "right": "right",
        "pageup": "pageup",
        "pagedown": "pagedown",
        "home": "home",
        "end": "end",
        "f1": "f1",
        "f2": "f2",
        "f3": "f3",
        "f4": "f4",
        "f5": "f5",
        "f6": "f6",
        "f7": "f7",
        "f8": "f8",
        "f9": "f9",
        "f10": "f10",
        "f11": "f11",
        "f12": "f12",
    }

    def _decode_keyseq_tokens(self, s: str) -> list[str]:
        """
        Decode a cgdb key-notation string into a list of Textual key-name
        tokens.  Examples::

            "<Esc>"        → ["escape"]
            "<C-w>"        → ["ctrl+w"]
            "<Space>:"     → ["space", ":"]
            "ab"           → ["a", "b"]
            "<CR>"         → ["enter"]
            "<F5>"         → ["f5"]
        """
        tokens: list[str] = []
        i = 0
        while i < len(s):
            if s[i] == "<":
                end = s.find(">", i)
                if end != -1:
                    name = s[i + 1 : end].lower()
                    tokens.append(self._key_token(name))
                    i = end + 1
                    continue
            ch = s[i]
            if ch == " ":
                tokens.append("space")
            else:
                tokens.append(ch)
            i += 1
        return tokens

    def _key_token(self, name: str) -> str:
        """Map a lower-cased key name (the part between < >) to a Textual key name."""
        if name in self._KEY_TOKENS:
            return self._KEY_TOKENS[name]
        # <C-x> → ctrl+x
        if name.startswith("c-") and len(name) == 3:
            return f"ctrl+{name[2].lower()}"
        # <S-x> → uppercase char (shift)
        if name.startswith("s-") and len(name) == 3:
            return name[2].upper()
        # <M-x> / <A-x> → meta: treat as Alt sequence; Textual rarely uses these natively
        if (name.startswith("m-") or name.startswith("a-")) and len(name) == 3:
            return f"escape+{name[2]}"
        # Unknown <Name> — return verbatim
        return f"<{name}>"
