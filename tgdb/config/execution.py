"""Command-dispatch helpers for the configuration package."""

from __future__ import annotations

import logging
import os
import re
import shlex
from pathlib import Path
from typing import Callable, Optional

from ..xdg_path import XDGPath

_log = logging.getLogger("tgdb.config")


class ConfigExecutionMixin:
    """Mixin providing file loading and command dispatch for ``ConfigParser``."""

    def default_rc_path(self) -> Optional[Path]:
        path = XDGPath.config_home() / "tgdb" / "tgdbrc"
        if path.exists():
            return path
        return None


    async def load_file_async(self, path: str, print_fn: Optional[Callable] = None) -> str | None:
        try:
            with open(path) as handle:
                raw_lines = handle.readlines()
        except OSError as exc:
            return f"source: cannot open '{path}': {exc}"

        _log.info(f"loading rc file: {path}")
        index = 0
        while index < len(raw_lines):
            line = raw_lines[index].rstrip("\n")
            stripped = line.strip()
            index += 1

            if not stripped:
                continue

            check = stripped.lstrip(":")
            match = re.match(r"^(python|py)\s+<<\s+(\S+)\s*$", check, re.IGNORECASE)
            if match:
                cmd_name = match.group(1).lower()
                marker = match.group(2)
                code_lines: list[str] = []
                while index < len(raw_lines):
                    code_line = raw_lines[index].rstrip("\n")
                    index += 1
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


    async def execute_async(self, line: str, print_fn: Optional[Callable] = None) -> str | None:
        line = line.strip()
        if not line:
            return None
        if line.startswith(":"):
            line = line[1:].strip()
        if not line or line.startswith("#"):
            return None

        py_match = re.match(
            r"^(python|pyfile|pyf|py)\s*(.*)",
            line,
            re.DOTALL | re.IGNORECASE,
        )
        if py_match:
            return await self._dispatch_python_command(py_match, print_fn)

        command_match = re.match(r"^command\b\s*(.*)", line, re.DOTALL | re.IGNORECASE)
        if command_match:
            return await self._cmd_command(command_match.group(1))

        eval_match = re.match(
            r"^(evaluate|signal)\b\s*(.*)",
            line,
            re.DOTALL | re.IGNORECASE,
        )
        if eval_match:
            eval_cmd = eval_match.group(1).lower()
            eval_expr = eval_match.group(2)
            handler = self._handlers.get(eval_cmd)
            if handler is not None:
                return handler([eval_expr] if eval_expr else [])
            return None

        map_match = re.match(
            r"^(imap|im|map)\b\s*(\S+)\s*(.*)",
            line,
            re.DOTALL | re.IGNORECASE,
        )
        if map_match:
            return self._dispatch_map_command(map_match)

        try:
            parts = shlex.split(line)
        except ValueError:
            parts = line.split()
        if not parts:
            return None

        raw_cmd = parts[0]
        cmd = raw_cmd.lower()
        args = parts[1:]

        if re.match(r"^[+-]?\d+$", raw_cmd):
            handler = self._handlers.get("_goto_line")
            if handler is not None:
                return handler([raw_cmd])
            return None

        if raw_cmd == "!!" or (raw_cmd.startswith("!") and raw_cmd[1:].isdigit()):
            return await self._cmd_history_run(raw_cmd)

        if cmd in self._handlers:
            return self._handlers[cmd](args)

        return await self._dispatch_builtin_command(cmd, raw_cmd, args, print_fn, line)


    async def _dispatch_python_command(self, match: re.Match, print_fn: Optional[Callable]) -> str | None:
        cmd = match.group(1).lower()
        raw_arg = match.group(2)
        if cmd in ("python", "py"):
            heredoc_match = re.match(r"^<<\s*(\S+)\s*\n(.*)", raw_arg, re.DOTALL)
            if heredoc_match:
                marker = heredoc_match.group(1)
                body_lines = heredoc_match.group(2).split("\n")
                code_lines: list[str] = []
                for body_line in body_lines:
                    if body_line.strip() == marker:
                        break
                    code_lines.append(body_line)
                raw_arg = "\n".join(code_lines)
            return await self._exec_py_async(raw_arg, "<tgdb:python>", print_fn)
        return await self._exec_pyfile_async(raw_arg.strip(), print_fn)


    def _dispatch_map_command(self, match: re.Match) -> str | None:
        map_cmd = match.group(1).lower()
        mode = "gdb" if map_cmd in ("imap", "im") else "tgdb"
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
    ) -> str | None:
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

        if raw_cmd[:1].isupper():
            user_command, amb_err = self._lookup_user_command(raw_cmd)
            if amb_err:
                return amb_err
            if user_command is not None:
                match = re.match(r"\S+\s*(.*)", raw_line, re.DOTALL)
                raw_args = match.group(1) if match else ""
                return await self._exec_user_command_async(
                    user_command,
                    raw_args,
                    print_fn=print_fn,
                )

        return f"Unknown command: {cmd}"
