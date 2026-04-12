"""
Python / pyfile execution helpers for the configuration package.

Provides the async helpers that compile and run user-supplied Python
code inside the persistent ``_py_namespace``.
"""

from __future__ import annotations

import asyncio
import builtins
import contextlib
import io
import os
import textwrap
import traceback
from pathlib import Path
from typing import Any, Callable, Optional

from .types import _TGDB_RESERVED_PREFIX


class PythonExecMixin:
    """Mixin that adds :python / :pyfile execution to ConfigParser."""

    _py_namespace: dict

    async def _exec_py_async(self, code: str, source_label: str, print_fn: Optional[Callable] = None) -> Optional[str]:
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

            def _custom_print(*args, sep: str = " ", end: str = "\n", file=None, flush: bool = False) -> None:
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
            self._py_namespace.update(
                {
                    k: v
                    for k, v in ns.items()
                    if not k.startswith(_TGDB_RESERVED_PREFIX) and k != "__builtins__"
                }
            )

        if isinstance(writer, io.StringIO):
            out = writer.getvalue().rstrip("\n")
            return out or None
        return None


    async def _exec_pyfile_async(self, path: str, print_fn: Optional[Callable] = None) -> Optional[str]:
        """Execute a Python file as an async coroutine."""
        if not path:
            return "pyfile: missing filename"
        path = os.path.expanduser(path)
        try:
            code = Path(path).read_text(encoding="utf-8")
        except OSError as e:
            return f"pyfile: cannot open '{path}': {e}"
        return await self._exec_py_async(code, path, print_fn)
