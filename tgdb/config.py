"""
Configuration parser for tgdb.

Supports $XDG_CONFIG_HOME/tgdb/tgdbrc (default: ~/.config/tgdb/tgdbrc)
as the initialization file.
Supports all :set options, :highlight, :map, :imap, :unmap, :iunmap.
"""
from __future__ import annotations

import asyncio
import builtins
import contextlib
import io
import json
import os
import re
import shlex
import textwrap
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .highlight_groups import HighlightGroups
    from .key_mapper import KeyMapper

from .xdg_path import XDGPath

# ---------------------------------------------------------------------------
# Reserved namespace prefix — any name starting with this string is internal
# and must never leak into or be overwritten from user Python scripts.
# ---------------------------------------------------------------------------
_TGDB_RESERVED_PREFIX = "_tgdb_RSVD"

# ---------------------------------------------------------------------------
# Config state
# ---------------------------------------------------------------------------

@dataclass
class Config:
    # Boolean options
    autosourcereload: bool = True
    color: bool = True
    debugwincolor: bool = True
    disasm: bool = False
    hlsearch: bool = False
    ignorecase: bool = False
    showmarks: bool = True
    showdebugcommands: bool = False
    timeout: bool = True
    ttimeout: bool = True
    wrapscan: bool = True

    # History options
    historysize: int = 1024         # set history=N  (0 = disabled)

    # Integer options
    scrollbackbuffersize: int = 10000
    tabstop: int = 8
    timeoutlen: int = 1000
    ttimeoutlen: int = 100
    winminheight: int = 0
    winminwidth: int = 0

    # String / enum options
    cgdbmodekey: str = "escape"     # key name
    executinglinedisplay: str = "longarrow"   # shortarrow|longarrow|highlight|block
    selectedlinedisplay: str = "block"
    winsplit: str = "even"          # src_full|src_big|even|gdb_big|gdb_full
    winsplitorientation: str = "vertical"   # horizontal|vertical
    syntax: str = "on"              # on|off|c|asm|…


# Abbreviation → canonical name
_ALIASES: dict[str, str] = {
    "asr": "autosourcereload",
    "arrowstyle": "executinglinedisplay",  # deprecated alias (cgdb cgdbrc.cpp)
    "as": "executinglinedisplay",         # short form of arrowstyle
    "dwc": "debugwincolor",
    "dis": "disasm",
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

_BOOL_OPTIONS = {
    "autosourcereload", "color", "debugwincolor", "disasm", "hlsearch",
    "ignorecase", "showmarks", "showdebugcommands", "timeout",
    "ttimeout", "wrapscan",
}
_INT_OPTIONS = {
    "historysize", "scrollbackbuffersize", "tabstop", "timeoutlen",
    "ttimeoutlen", "winminheight", "winminwidth",
}
_STR_OPTIONS = {
    "cgdbmodekey", "executinglinedisplay", "selectedlinedisplay",
    "winsplit", "winsplitorientation", "syntax",
}

# Valid -nargs values for :command
_VALID_NARGS = {"0", "1", "*", "?", "+"}

# User command name must start with uppercase, rest uppercase/lowercase/digits
_CMD_NAME_RE = re.compile(r'^[A-Z][A-Za-z0-9]*$')


@dataclass
class UserCommandDef:
    """One user-defined command registered via :command."""
    name: str
    nargs: str          # "0" | "1" | "*" | "?" | "+"
    complete_func: str  # name of Python function in _py_namespace, or ""
    replacement: str    # raw replacement template with <args>/<q-args>/<f-args>/<lt>


class ConfigParser:
    """
    Parses cgdbrc-style config commands and updates a Config object.

    Pass in the live Config, HighlightGroups, and KeyMapper objects.
    """

    def __init__(self,
                 config: Config,
                 highlight_groups: "HighlightGroups",
                 key_mapper: "KeyMapper") -> None:
        self.config = config
        self.hl = highlight_groups
        self.km = key_mapper
        # Additional command handlers registered by the app
        self._handlers: dict[str, Callable[[list[str]], Optional[str]]] = {}
        # Persistent namespace shared across all :python / :pyfile calls
        self._py_namespace: dict = {
            "__builtins__": builtins,
            "config": self.config,
            "hl": self.hl,
            "km": self.km,
        }
        # User-defined commands registered via :command
        self._user_commands: dict[str, UserCommandDef] = {}
        # Recursion depth guard for user command expansion
        self._exec_depth: int = 0
        # Reference to the CommandLineBar — set by TGDBApp so that
        # "save history" and history-size changes can reach the widget.
        self._cmdline_bar = None

    def set_cmdline_bar(self, bar) -> None:
        """Inject a reference to the CommandLineBar for history operations."""
        self._cmdline_bar = bar

    def register_handler(self, name: str,
                         fn: Callable[[list[str]], Optional[str]]) -> None:
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
        return path if path.exists() else None

    async def load_file_async(self, path: str,
                              print_fn: Optional[Callable] = None) -> Optional[str]:
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

        i = 0
        while i < len(raw_lines):
            line = raw_lines[i].rstrip("\n")
            stripped = line.strip()
            i += 1

            if not stripped:
                continue

            # Detect heredoc: [:]python << MARKER
            check = stripped.lstrip(":")
            m = re.match(r'^(python|py)\s+<<\s+(\S+)\s*$', check, re.IGNORECASE)
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

    async def execute_async(self, line: str, print_fn: Optional[Callable] = None) -> Optional[str]:
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
        if not line:
            return None

        # Comment: leading whitespace + '#' is a no-op.
        if re.match(r'^\s*#', line):
            return None

        # :python / :py and :pyfile / :pyf take raw (un-tokenised) argument.
        _raw = re.match(r'^(python|pyfile|pyf|py)\s*(.*)', line, re.DOTALL | re.IGNORECASE)
        if _raw:
            _cmd, _raw_arg = _raw.group(1).lower(), _raw.group(2)
            if _cmd in ("python", "py"):
                # Handle heredoc format: "<<  MARKER\n...\nMARKER"
                _hd = re.match(r'^<<\s*(\S+)\s*\n(.*)', _raw_arg, re.DOTALL)
                if _hd:
                    _marker, _body = _hd.group(1), _hd.group(2)
                    # Strip the closing marker line
                    _body_lines = _body.split("\n")
                    code_lines = []
                    for bl in _body_lines:
                        if bl.strip() == _marker:
                            break
                        code_lines.append(bl)
                    _raw_arg = "\n".join(code_lines)
                return await self._exec_py_async(_raw_arg, "<tgdb:python>", print_fn)
            else:
                return await self._exec_pyfile_async(_raw_arg.strip(), print_fn)

        # :command needs raw handling to preserve the replacement template.
        _cmd_m = re.match(r'^command\b\s*(.*)', line, re.DOTALL | re.IGNORECASE)
        if _cmd_m:
            return await self._cmd_command(_cmd_m.group(1))

        # :map / :imap need raw handling so spaces in the RHS are preserved.
        _map_m = re.match(r'^(imap|im|map)\b\s*(\S+)\s*(.*)', line, re.DOTALL | re.IGNORECASE)
        if _map_m:
            _mcmd = _map_m.group(1).lower()
            _mode = "gdb" if _mcmd in ("imap", "im") else "tgdb"
            _lhs = self._decode_keyseq_tokens(_map_m.group(2))
            _rhs = self._decode_keyseq_tokens(_map_m.group(3))
            if not _lhs:
                return "map: empty lhs"
            self.km.map(_mode, _lhs, _rhs)
            return None

        try:
            parts = shlex.split(line)
        except ValueError:
            parts = line.split()
        if not parts:
            return None

        raw_cmd = parts[0]      # original case — needed for user-command lookup
        cmd = raw_cmd.lower()
        args = parts[1:]

        # Pure number: :12 → goto line; :+5 → scroll down; :-3 → scroll up
        if re.match(r'^[+-]?\d+$', raw_cmd):
            handler = self._handlers.get("_goto_line")
            if handler is not None:
                return handler([raw_cmd])
            return None

        # History replay: !! → last entry; !N → entry N
        if raw_cmd == "!!" or (raw_cmd.startswith("!") and raw_cmd[1:].isdigit()):
            return await self._cmd_history_run(raw_cmd)

        # Registered handlers (sync callables — no await needed)
        if cmd in self._handlers:
            return self._handlers[cmd](args)

        # Built-in commands
        if cmd == "set":
            return await self._cmd_set(args)
        elif cmd in ("highlight", "hi"):
            return await self._cmd_highlight(args)
        elif cmd in ("unmap", "unm"):
            return await self._cmd_unmap("tgdb", args)
        elif cmd in ("iunmap", "iu"):
            return await self._cmd_unmap("gdb", args)
        elif cmd == "noh":
            self.config.hlsearch = False
            return None
        elif cmd == "syntax":
            if args:
                return await self._set_option("syntax", args[0])
            return None
        elif cmd in ("source", "so"):
            if not args:
                return "source: missing filename"
            return await self.load_file_async(
                os.path.expanduser(args[0]),
                print_fn=print_fn,
            )
        elif cmd == "save":
            return await self._cmd_save(args)
        elif cmd == "history":
            return await self._cmd_history()

        # User-defined commands (must start with an uppercase letter)
        if raw_cmd[:1].isupper():
            ucmd, amb_err = self._lookup_user_command(raw_cmd)
            if amb_err:
                return amb_err
            if ucmd is not None:
                m = re.match(r'\S+\s*(.*)', line, re.DOTALL)
                raw_args = m.group(1) if m else ""
                return await self._exec_user_command_async(
                    ucmd,
                    raw_args,
                    print_fn=print_fn,
                )

        return f"Unknown command: {cmd}"

    async def _exec_py_async(self, code: str, source_label: str,
                             print_fn: Optional[Callable] = None) -> Optional[str]:
        """Compile *code* as ``async def _tgdb_RSVD_run_script()`` and await it.

        This lets scripts use ``await tgdb.screen.split(...)`` etc.
        Each ``print()`` call immediately forwards the text to *print_fn*
        (the CommandLineBar's append_output) so output appears as soon as
        the event loop gets a cycle.
        """
        if not code.strip():
            return None

        # Indent and wrap in an async def so the user can use await freely.
        # Inject a 'finally' block that copies function-local names back to
        # globals (== ns) so that top-level 'def foo', 'import x', 'x = 1'
        # survive into the persistent namespace — same as the sync exec() path.

        # ensure the user's code itself is properly de-indented
        # in case they pasted it with leading tabs/spaces
        user_code = textwrap.dedent(code)

        # re-indent the user's code by exactly 8 spaces (2 levels: one for 'def', one for 'try')
        indented_user_code = textwrap.indent(user_code, "        ")
        wrapper = f"""\
async def {_TGDB_RESERVED_PREFIX}_run_script():
    try:
{indented_user_code}
    finally:
        {_TGDB_RESERVED_PREFIX}_locs = locals()
        globals().update({{k: v for k, v in {_TGDB_RESERVED_PREFIX}_locs.items() if not k.startswith('{_TGDB_RESERVED_PREFIX}')}})
"""
        try:
            compiled = compile(wrapper, source_label, "exec")
        except SyntaxError:
            return traceback.format_exc().strip()

        ns = dict(self._py_namespace)

        # Install a custom print() that forwards output to print_fn immediately.
        # A plain sys.stdout redirect is also set up to catch output from
        # imported modules (e.g. third-party libraries that call sys.stdout.write).
        class _Writer:
            def __init__(self, fn: Callable) -> None:
                self._fn = fn

            def write(self, s: str) -> int:
                if s:
                    self._fn(s)
                return len(s)

            def flush(self) -> None:
                pass

            def isatty(self) -> bool:
                return False

            def readable(self) -> bool:
                return False

            def writable(self) -> bool:
                return True

            def seekable(self) -> bool:
                return False

        if print_fn is not None:
            writer: Any = _Writer(print_fn)

            def _custom_print(*args, sep: str = " ", end: str = "\n",
                              file=None, flush: bool = False) -> None:
                print_fn(sep.join(str(a) for a in args) + end)

            raw_builtins = ns.get("__builtins__", builtins)
            if isinstance(raw_builtins, dict):
                builtins_proxy = dict(raw_builtins)
            else:
                builtins_proxy = dict(vars(raw_builtins))
            builtins_proxy["print"] = _custom_print
            ns["__builtins__"] = builtins_proxy
        else:
            writer = io.StringIO()

        try:
            exec(compiled, ns)  # noqa: S102 — defines _tgdb_RSVD_run_script in ns
            script_fn = ns.get(f"{_TGDB_RESERVED_PREFIX}_run_script")
            if script_fn is None:
                return f"Internal error: {_TGDB_RESERVED_PREFIX}_run_script not defined after exec"
            with contextlib.redirect_stdout(writer), contextlib.redirect_stderr(writer):
                await script_fn()
        except asyncio.CancelledError:
            raise
        except Exception:
            err = traceback.format_exc().strip()
            if print_fn:
                print_fn(err)
                return None
            return err
        finally:
            # Propagate any new/modified names back to the persistent namespace
            # so that 'def foo', 'import mod', 'x = 1' survive across commands.
            self._py_namespace.update({k: v for k, v in ns.items()
                                        if not k.startswith(_TGDB_RESERVED_PREFIX)
                                        and k != "__builtins__"})

        if isinstance(writer, io.StringIO):
            out = writer.getvalue().rstrip("\n")
            return out or None
        return None

    async def _exec_pyfile_async(self, path: str,
                                 print_fn: Optional[Callable] = None) -> Optional[str]:
        """Execute a Python file as an async coroutine."""
        if not path:
            return "pyfile: missing filename"
        path = os.path.expanduser(path)
        try:
            code = Path(path).read_text(encoding="utf-8")
        except OSError as e:
            return f"pyfile: cannot open '{path}': {e}"
        return await self._exec_py_async(code, path, print_fn)

    # ------------------------------------------------------------------
    # :command — user-defined commands
    # ------------------------------------------------------------------

    async def _cmd_command(self, raw: str) -> Optional[str]:
        """Parse and handle a :command invocation.

        :command              → list all user commands
        :command {Prefix}     → list user commands starting with Prefix
        :command [attr...] {Name} {repl}  → define a new user command
        """
        raw = raw.strip()
        if not raw:
            return self._list_user_commands("")
        remaining = raw
        nargs = "0"
        complete_func = ""
        while remaining.startswith("-"):
            m = re.match(r'-nargs=([01*?+])\s*', remaining)
            if m:
                nargs = m.group(1)
                remaining = remaining[m.end():]
                continue
            m = re.match(r'-complete=(\S+)\s*', remaining)
            if m:
                complete_func = m.group(1)
                remaining = remaining[m.end():]
                continue
            m = re.match(r'-bang\s*', remaining)
            if m:
                remaining = remaining[m.end():]
                continue
            m = re.match(r'(-\S+)', remaining)
            token = m.group(1) if m else remaining.split()[0]
            return f"command: unknown attribute: {token!r}"
        m = re.match(r'(\S+)\s*', remaining)
        if not m:
            return self._list_user_commands("")
        name = m.group(1)
        after_name = remaining[m.end():]
        if not _CMD_NAME_RE.match(name):
            return (f"command: name must start with an uppercase letter and contain "
                    f"only letters/digits: {name!r}")
        if not after_name.strip():
            return self._list_user_commands(name)
        if complete_func and nargs == "0":
            return "command: -complete requires -nargs (nargs=0 means no arguments)"
        if name in self._user_commands:
            return f"command: '{name}' already exists"
        self._user_commands[name] = UserCommandDef(
            name=name,
            nargs=nargs,
            complete_func=complete_func,
            replacement=after_name,
        )
        return None

    def _list_user_commands(self, prefix: str) -> Optional[str]:
        matches = {n: c for n, c in self._user_commands.items() if n.startswith(prefix)}
        if not matches:
            if not prefix:
                return None
            return f"command: no user commands matching '{prefix}'"
        w_name = max(max(len(n) for n in matches), 4)
        header = f"{'Name':<{w_name}}  Nargs  Complete              Definition"
        sep = "-" * max(len(header), 60)
        lines = [header, sep]
        for n in sorted(matches):
            c = matches[n]
            lines.append(f"{n:<{w_name}}  {c.nargs:<6} {c.complete_func:<20}  {c.replacement}")
        return "\n".join(lines)

    def _lookup_user_command(self, name: str) -> tuple[Optional[UserCommandDef], Optional[str]]:
        if name in self._user_commands:
            return self._user_commands[name], None
        matches = [n for n in self._user_commands if n.startswith(name)]
        if not matches:
            return None, None
        if len(matches) == 1:
            return self._user_commands[matches[0]], None
        return None, f"Ambiguous command: '{name}' (matches: {', '.join(sorted(matches))})"

    async def _exec_user_command_async(self, ucmd: UserCommandDef, raw_args: str,
                                       print_fn: Optional[Callable] = None) -> Optional[str]:
        if self._exec_depth >= 20:
            return f"{ucmd.name}: maximum command recursion depth exceeded"
        try:
            shlex_args = shlex.split(raw_args)
        except ValueError:
            shlex_args = raw_args.split()
        err = self._validate_nargs(ucmd.nargs, shlex_args, raw_args)
        if err:
            return f"{ucmd.name}: {err}"
        try:
            expanded = self._expand_replacement(ucmd.replacement, shlex_args, raw_args)
        except Exception as e:
            return f"{ucmd.name}: error expanding replacement: {e}"
        self._exec_depth += 1
        try:
            return await self.execute_async(expanded, print_fn=print_fn)
        finally:
            self._exec_depth -= 1

    def _validate_nargs(self, nargs: str, shlex_args: list[str], raw_args: str) -> Optional[str]:
        stripped = raw_args.strip()
        if nargs == "0":
            if stripped:
                return "no arguments allowed"
        elif nargs == "1":
            if not stripped:
                return "exactly one argument required"
        elif nargs == "?":
            if len(shlex_args) > 1:
                return "at most one argument allowed"
        elif nargs == "+":
            if not shlex_args:
                return "at least one argument required"
        return None

    def _expand_replacement(self, template: str, shlex_args: list[str], raw_args: str) -> str:
        args_str = raw_args.strip()
        q_args_str = json.dumps(args_str) if args_str else '""'
        f_parts = self._f_args_split(args_str)
        f_args_str = ",".join(json.dumps(a) for a in f_parts) if f_parts else ""

        def replacer(m: re.Match) -> str:
            token = m.group(1).lower()
            if token == "args":
                return args_str
            if token == "q-args":
                return q_args_str
            if token == "f-args":
                return f_args_str
            if token == "lt":
                return "<"
            return m.group(0)

        return re.sub(r'<([^>]+)>', replacer, template)

    @staticmethod
    def _f_args_split(text: str) -> list[str]:
        """Split text into f-args tokens with backslash escaping.

        Rules:
          \\\\  → single backslash
          \\(space) → literal space (no split)
          \\X  → \\X unchanged
          unescaped space/tab → argument separator
        """
        args: list[str] = []
        current: list[str] = []
        i = 0
        while i < len(text):
            c = text[i]
            if c == "\\" and i + 1 < len(text):
                nc = text[i + 1]
                if nc == "\\":
                    current.append("\\")
                    i += 2
                elif nc in (" ", "\t"):
                    current.append(nc)
                    i += 2
                else:
                    current.append(c)
                    current.append(nc)
                    i += 2
            elif c in (" ", "\t"):
                if current:
                    args.append("".join(current))
                    current = []
                while i < len(text) and text[i] in (" ", "\t"):
                    i += 1
            else:
                current.append(c)
                i += 1
        if current:
            args.append("".join(current))
        return args

    def get_completions(self, arg_lead: str, cmd_line: str, cursor_pos: int) -> list[str]:
        """Return completion candidates for Tab completion in the status bar."""
        line = cmd_line.lstrip(":")
        m = re.match(r'([A-Z][A-Za-z0-9]*)', line)
        if not m:
            return []
        cmd_name = m.group(1)
        ucmd, _ = self._lookup_user_command(cmd_name)
        if ucmd is None or not ucmd.complete_func:
            return []
        fn = self._py_namespace.get(ucmd.complete_func)
        if not callable(fn):
            return []
        try:
            result = fn(arg_lead, cmd_line, cursor_pos)
            if isinstance(result, (list, tuple)):
                return [str(s) for s in result]
        except Exception:
            pass
        return []

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

    async def _set_option(self, name: str, value: str) -> Optional[str]:
        name = self._resolve_name(name)
        if name in _BOOL_OPTIONS:
            setattr(self.config, name, value.lower() not in ("0", "false", "off", "no"))
            return None
        elif name in _INT_OPTIONS:
            try:
                setattr(self.config, name, int(value))
                # Propagate timeout values to key mapper
                if name == "timeoutlen":
                    self.km.timeout_ms = int(value)
                elif name == "ttimeoutlen":
                    self.km.ttimeout_ms = int(value)
            except ValueError:
                return f"set: invalid integer '{value}'"
            return None
        elif name in _STR_OPTIONS:
            setattr(self.config, name, value.lower())
            return None
        return f"set: unknown option '{name}'"

    def _resolve_name(self, name: str) -> str:
        return _ALIASES.get(name.lower(), name.lower())

    # ------------------------------------------------------------------
    # :save
    # ------------------------------------------------------------------

    async def _cmd_save(self, args: list[str]) -> Optional[str]:
        """Handle: save history [filename]"""
        if not args or args[0].lower() != "history":
            return f"save: unknown sub-command '{args[0] if args else ''}'"
        bar = self._cmdline_bar
        if bar is None:
            return "save history: command-line bar not available"
        path = Path(os.path.expanduser(args[1])) if len(args) >= 2 else None
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
            return f"unmap: no such mapping"
        return None

    # ------------------------------------------------------------------
    # Key sequence decoding — cgdb notation → Textual key-name token list
    # ------------------------------------------------------------------

    # Maps lower-cased cgdb <Name> identifiers to Textual event.key strings
    _KEY_TOKENS: dict[str, str] = {
        "space": "space",
        "enter": "enter", "return": "enter", "cr": "enter",
        "nl": "ctrl+j",
        "tab": "tab",
        "esc": "escape", "escape": "escape",
        "bs": "backspace", "backspace": "backspace",
        "del": "delete", "delete": "delete",
        "insert": "insert",
        "nul": "ctrl+@",
        "lt": "<",
        "bslash": "\\",
        "bar": "|",
        "up": "up", "down": "down", "left": "left", "right": "right",
        "pageup": "pageup", "pagedown": "pagedown",
        "home": "home", "end": "end",
        "f1": "f1", "f2": "f2", "f3": "f3", "f4": "f4",
        "f5": "f5", "f6": "f6", "f7": "f7", "f8": "f8",
        "f9": "f9", "f10": "f10", "f11": "f11", "f12": "f12",
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
                    name = s[i + 1:end].lower()
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
