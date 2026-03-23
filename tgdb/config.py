"""
Configuration parser — mirrors cgdb's cgdbrc.cpp.

Parses ~/.cgdb/cgdbrc (or $CGDB_DIR/cgdbrc).
Supports all :set options, :highlight, :map, :imap, :unmap, :iunmap.
"""
from __future__ import annotations

import builtins
import contextlib
import io
import json
import os
import re
import shlex
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .highlight_groups import HighlightGroups
    from .key_mapper import KeyMapper


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
    "ignorecase", "showmarks", "showdebugcommands", "timeout", "ttimeout",
    "wrapscan",
}
_INT_OPTIONS = {
    "scrollbackbuffersize", "tabstop", "timeoutlen", "ttimeoutlen",
    "winminheight", "winminwidth",
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

    def load_default_rc(self) -> None:
        cgdb_dir = os.environ.get("CGDB_DIR", "")
        candidates = []
        if cgdb_dir:
            candidates.append(Path(cgdb_dir) / "cgdbrc")
        home = Path.home()
        candidates.append(home / ".cgdb" / "cgdbrc")
        for path in candidates:
            if path.exists():
                self.load_file(str(path))
                return

    def load_file(self, path: str) -> Optional[str]:
        """Load and execute every non-blank, non-comment line in *path*.

        Returns an error string if the file cannot be opened, None on success.
        Errors from individual lines are silently ignored (same as startup behaviour).
        """
        try:
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    self.execute(line)
        except OSError as e:
            return f"source: cannot open '{path}': {e}"
        return None

    # ------------------------------------------------------------------
    # Command execution
    # ------------------------------------------------------------------

    def execute(self, line: str) -> Optional[str]:
        """
        Execute one config/status-bar command.
        Returns an error string or None on success.
        """
        line = line.strip()
        if not line:
            return None
        # Strip leading colon
        if line.startswith(":"):
            line = line[1:].strip()
        if not line:
            return None

        # :python / :py and :pyfile / :pyf take a raw (un-tokenised) argument,
        # so handle them before shlex to preserve spaces inside code strings.
        _raw = re.match(r'^(python|pyfile|pyf|py)\s*(.*)', line, re.DOTALL | re.IGNORECASE)
        if _raw:
            _cmd, _raw_arg = _raw.group(1).lower(), _raw.group(2)
            if _cmd in ("python", "py"):
                return self._cmd_python(_raw_arg)
            else:
                return self._cmd_pyfile(_raw_arg.strip())

        # :command also needs raw handling to preserve the replacement template.
        _cmd_m = re.match(r'^command\b\s*(.*)', line, re.DOTALL | re.IGNORECASE)
        if _cmd_m:
            return self._cmd_command(_cmd_m.group(1))

        try:
            parts = shlex.split(line)
        except ValueError:
            parts = line.split()
        if not parts:
            return None

        raw_cmd = parts[0]      # original case — needed for user-command lookup
        cmd = raw_cmd.lower()
        args = parts[1:]

        # Check registered handlers first
        if cmd in self._handlers:
            return self._handlers[cmd](args)

        # Built-in commands
        if cmd == "set":
            return self._cmd_set(args)
        elif cmd in ("highlight", "hi"):
            return self._cmd_highlight(args)
        elif cmd in ("map",):
            return self._cmd_map("cgdb", args)
        elif cmd in ("imap", "im"):
            return self._cmd_map("gdb", args)
        elif cmd in ("unmap", "unm"):
            return self._cmd_unmap("cgdb", args)
        elif cmd in ("iunmap", "iu"):
            return self._cmd_unmap("gdb", args)
        elif cmd == "noh":
            self.config.hlsearch = False
            return None
        elif cmd == "syntax":
            if args:
                return self._set_option("syntax", args[0])
        elif cmd in ("source", "so"):
            if not args:
                return "source: missing filename"
            return self.load_file(os.path.expanduser(args[0]))

        # User-defined commands (must start with an uppercase letter)
        if raw_cmd[:1].isupper():
            ucmd, amb_err = self._lookup_user_command(raw_cmd)
            if amb_err:
                return amb_err
            if ucmd is not None:
                m = re.match(r'\S+\s*(.*)', line, re.DOTALL)
                raw_args = m.group(1) if m else ""
                return self._exec_user_command(ucmd, raw_args)

        return f"Unknown command: {cmd}"

    # ------------------------------------------------------------------
    # :python / :pyfile
    # ------------------------------------------------------------------

    def _exec_py(self, code: str, source_label: str) -> Optional[str]:
        """Execute *code* in the persistent namespace; return captured output or error."""
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
                exec(compile(code, source_label, "exec"), self._py_namespace)  # noqa: S102
        except Exception:
            return traceback.format_exc().strip()
        output = buf.getvalue()
        return output.rstrip("\n") or None

    def _cmd_python(self, code: str) -> Optional[str]:
        """Execute a Python code string in the persistent tgdb namespace."""
        if not code.strip():
            return None
        return self._exec_py(code, "<tgdb:python>")

    def _cmd_pyfile(self, path: str) -> Optional[str]:
        """Execute a Python file in the persistent tgdb namespace."""
        if not path:
            return "pyfile: missing filename"
        path = os.path.expanduser(path)
        try:
            code = Path(path).read_text(encoding="utf-8")
        except OSError as e:
            return f"pyfile: cannot open '{path}': {e}"
        return self._exec_py(code, path)

    # ------------------------------------------------------------------
    # :command — user-defined commands
    # ------------------------------------------------------------------

    def _cmd_command(self, raw: str) -> Optional[str]:
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

    def _exec_user_command(self, ucmd: UserCommandDef, raw_args: str) -> Optional[str]:
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
            return self.execute(expanded)
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

    def _cmd_set(self, args: list[str]) -> Optional[str]:
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
            return self._set_option(name, value.strip())
        # Boolean enable
        name = self._resolve_name(expr)
        if name in _BOOL_OPTIONS:
            setattr(self.config, name, True)
            return None
        return f"set: unknown option '{expr}'"

    def _set_option(self, name: str, value: str) -> Optional[str]:
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
    # :highlight / :hi
    # ------------------------------------------------------------------

    def _cmd_highlight(self, args: list[str]) -> Optional[str]:
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
    # :map / :imap
    # ------------------------------------------------------------------

    def _cmd_map(self, mode: str, args: list[str]) -> Optional[str]:
        if len(args) < 2:
            return "map: requires lhs and rhs"
        lhs = self._decode_keyseq(args[0])
        rhs = self._decode_keyseq(args[1])
        self.km.map(mode, lhs, rhs)
        return None

    def _cmd_unmap(self, mode: str, args: list[str]) -> Optional[str]:
        if not args:
            return "unmap: requires lhs"
        lhs = self._decode_keyseq(args[0])
        self.km.unmap(mode, lhs)
        return None

    def _decode_keyseq(self, s: str) -> str:
        """
        Decode cgdb key notation: <F5>, <C-w>, <Space>, <Enter>, etc.
        Returns a single string of "logical" key characters.
        """
        result = []
        i = 0
        while i < len(s):
            if s[i] == "<":
                end = s.find(">", i)
                if end != -1:
                    token = s[i + 1:end].lower()
                    result.append(self._keyname(token))
                    i = end + 1
                    continue
            result.append(s[i])
            i += 1
        return "".join(result)

    _KEY_NAMES = {
        "space": " ", "enter": "\r", "return": "\r", "cr": "\r",
        "nl": "\n", "tab": "\t", "esc": "\x1b", "escape": "\x1b",
        "bs": "\x08", "backspace": "\x08",
        "del": "\x7f", "delete": "\x7f",
        "insert": "\x1b[2~",
        "nul": "\x00",
        "ff": "\x0c",           # formfeed
        "lt": "<",              # less-than
        "bslash": "\\",         # backslash
        "bar": "|",             # vertical bar
        "up": "\x1b[A", "down": "\x1b[B", "right": "\x1b[C", "left": "\x1b[D",
        "pageup": "\x1b[5~", "pagedown": "\x1b[6~",
        "home": "\x1b[H", "end": "\x1b[F",
        "f1": "\x1bOP", "f2": "\x1bOQ", "f3": "\x1bOR", "f4": "\x1bOS",
        "f5": "\x1b[15~", "f6": "\x1b[17~", "f7": "\x1b[18~",
        "f8": "\x1b[19~", "f9": "\x1b[20~", "f10": "\x1b[21~",
        "f11": "\x1b[23~", "f12": "\x1b[24~",
    }

    def _keyname(self, token: str) -> str:
        if token in self._KEY_NAMES:
            return self._KEY_NAMES[token]
        # C-x → Ctrl char (e.g. <C-a> → 0x01)
        if token.startswith("c-") and len(token) == 3:
            ch = token[2]
            return chr(ord(ch.upper()) - 64)
        # S-x → shifted char (e.g. <S-a> → 'A')  cgdb: shift keys
        if token.startswith("s-") and len(token) == 3:
            return token[2].upper()
        # M-x → Alt (ESC + char)
        if token.startswith("m-") and len(token) == 3:
            return "\x1b" + token[2]
        return token
