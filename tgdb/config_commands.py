"""
User-defined command mixin for ConfigParser.

Provides :command registration, lookup, expansion, execution,
and Tab-completion support.
"""

from __future__ import annotations

import json
import re
import shlex
from typing import Callable, Optional

from .config_types import _CMD_NAME_RE, UserCommandDef


class UserCommandMixin:
    """Mixin that adds :command user-defined command support to ConfigParser."""

    _user_commands: dict[str, UserCommandDef]
    _exec_depth: int
    _py_namespace: dict

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
            m = re.match(r"-nargs=([01*?+])\s*", remaining)
            if m:
                nargs = m.group(1)
                remaining = remaining[m.end() :]
                continue
            m = re.match(r"-complete=(\S+)\s*", remaining)
            if m:
                complete_func = m.group(1)
                remaining = remaining[m.end() :]
                continue
            m = re.match(r"-bang\s*", remaining)
            if m:
                remaining = remaining[m.end() :]
                continue
            m = re.match(r"(-\S+)", remaining)
            token = m.group(1) if m else remaining.split()[0]
            return f"command: unknown attribute: {token!r}"
        m = re.match(r"(\S+)\s*", remaining)
        if not m:
            return self._list_user_commands("")
        name = m.group(1)
        after_name = remaining[m.end() :]
        if not _CMD_NAME_RE.match(name):
            return f"command: name must start with an uppercase letter and contain only letters/digits: {name!r}"
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

    async def _exec_user_command_async(self, ucmd: UserCommandDef, raw_args: str, print_fn: Optional[Callable] = None) -> Optional[str]:
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

        return re.sub(r"<([^>]+)>", replacer, template)

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
        m = re.match(r"([A-Z][A-Za-z0-9]*)", line)
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
