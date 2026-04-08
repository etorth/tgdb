"""
VarobjMixin — varobj-related async MI commands.

Provides var_create, var_list_children, var_delete, var_update, and the
underlying mi_command_async helper.  Mixed into GDBController.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os

_log = logging.getLogger("tgdb.gdb_varobj")

# Python script run inside GDB to collect DWARF declaration lines for all
# local variables in the current function.
#
# We base64-encode it so the MI command stays a single safe ASCII string —
# no newlines, quotes, or backslashes that would confuse the GDB MI parser.
#
# Algorithm:
#   1. Walk UP from the current block to find the enclosing function block.
#   2. Scan every DWARF block inside the function's PC range by jumping from
#      one block's end address to the next (covers sibling blocks that GCC
#      emits for variables declared after goto labels, etc.).
#   3. For each block, also walk UP to collect variables in enclosing scopes.
#   4. Store the result in the GDB convenience variable $tgdb_decls so it can
#      be read back via a normal -data-evaluate-expression MI command.
_DECL_LINES_SCRIPT = """\
import gdb
frame = gdb.selected_frame()
fb = frame.block()
while fb and not fb.function:
    fb = fb.superblock
fs = fb.start
fe = fb.end
seen = set()
decls = {}
pc = fs
while pc < fe:
    b = gdb.block_for_pc(pc)
    if b is None or b.end <= pc:
        break
    cur = b
    while cur and cur.start >= fs:
        if cur.start not in seen:
            seen.add(cur.start)
            for s in cur:
                if s.is_variable and s.name not in decls:
                    decls[s.name] = s.line
        if cur.function:
            break
        cur = cur.superblock
    pc = b.end
gdb.set_convenience_variable("tgdb_decls", ",".join(f"{n}:{l}" for n, l in decls.items()))
"""

# Compute once at import time — avoids re-encoding on every call.
_DECL_LINES_B64 = base64.b64encode(_DECL_LINES_SCRIPT.encode()).decode()


class VarobjMixin:
    """Mixin providing varobj commands and the async MI command helper.

    Expects the host class to have:
        _mi_master_fd : int
        _token        : int
        _pending      : dict[int, asyncio.Future]
        _request_meta : dict[int, dict[str, object]]
    """

    # -- async MI command helper -------------------------------------------

    async def mi_command_async(self, cmd: str, timeout: float | None = 10.0) -> dict:
        """Send an MI command and await its result.

        Returns ``{"message": str, "payload": dict|None}``.
        Raises ``RuntimeError`` on send failure, ``^error`` response, or timeout.

        *timeout* is the number of seconds to wait for a GDB response.
        Pass ``None`` to wait forever (e.g. for slow operations like
        ``-file-list-exec-source-files`` on large binaries).

        The default 10-second timeout guards against GDB hanging inside a
        pretty-printer — for example when a variable is uninitialized and its
        garbage memory makes GDB think it has billions of children.
        """
        if self._mi_master_fd < 0:
            raise RuntimeError("MI channel not open")

        token = self._token
        self._token += 1
        self._request_meta[token] = {"report_error": False, "kind": None}

        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[token] = fut

        try:
            os.write(self._mi_master_fd, f"{token}{cmd}\n".encode())
        except OSError as e:
            self._request_meta.pop(token, None)
            self._pending.pop(token, None)
            _log.error("MI write failed: %s", e)
            raise RuntimeError(f"MI write failed: {e}") from e

        _log.debug("MI >> %s", cmd)

        if timeout is None:
            result = await fut
        else:
            try:
                result = await asyncio.wait_for(asyncio.shield(fut), timeout=timeout)
            except asyncio.TimeoutError:
                # GDB is stuck (e.g. iterating garbage children).  Remove the
                # pending entry so the eventual stale response is silently dropped.
                self._pending.pop(token, None)
                self._request_meta.pop(token, None)
                _log.warning("MI command timed out: %s", cmd)
                raise RuntimeError("MI command timed out — GDB may be busy")

        _log.debug("MI << token=%d msg=%s", token, result.get("message"))

        if result.get("message") == "error":
            payload = result.get("payload") or {}
            raise RuntimeError(payload.get("msg", "unknown MI error"))
        return result

    # -- varobj commands ---------------------------------------------------

    async def var_create(self, expr: str, *, frame: str = "*") -> dict:
        """Create a varobj for *expr*.  Returns the MI result payload.

        Keys include ``name``, ``numchild``, ``value``, ``type``, etc.
        """
        result = await self.mi_command_async(f'-var-create - {frame} "{expr}"')
        payload = result.get("payload") or {}
        _log.debug("var_create expr=%r -> name=%r", expr, payload.get("name"))
        return payload

    async def var_list_children(
        self, varobj_name: str, from_idx: int = 0, limit: int = 0
    ) -> tuple[list[dict], bool]:
        """List children of *varobj_name*.

        Returns ``(children, has_more)`` where *has_more* is True when GDB
        signalled that there are additional children beyond the fetched range.

        Each child dict has keys: ``name``, ``exp``, ``numchild``, ``value``,
        ``type``, etc.

        *from_idx* is the start index into the child list.
        *limit* is the maximum number of children to fetch.  When 0 (the
        default) all children are fetched without a range argument (original
        GDB behaviour — may be slow or hang for garbage-initialized containers
        with huge reported sizes).
        """
        if limit > 0:
            cmd = (
                f"-var-list-children --all-values"
                f" {varobj_name} {from_idx} {from_idx + limit}"
            )
        else:
            cmd = f"-var-list-children --all-values {varobj_name}"
        result = await self.mi_command_async(cmd)
        payload = result.get("payload") or {}
        has_more = payload.get("has_more", "0") == "1"
        children_raw = payload.get("children", [])
        children: list[dict] = []
        if isinstance(children_raw, list):
            for item in children_raw:
                if isinstance(item, dict):
                    child = item.get("child", item)
                    if isinstance(child, dict):
                        children.append(child)
        elif isinstance(children_raw, dict):
            child = children_raw.get("child", children_raw)
            if isinstance(child, dict):
                children.append(child)
        _log.debug(
            "var_list_children %s from=%d limit=%d -> %d children has_more=%s",
            varobj_name,
            from_idx,
            limit,
            len(children),
            has_more,
        )
        return children, has_more

    async def var_delete(self, varobj_name: str) -> None:
        """Delete a varobj and its children."""
        _log.debug("var_delete %s", varobj_name)
        try:
            await self.mi_command_async(f"-var-delete {varobj_name}")
        except RuntimeError:
            pass

    async def get_decl_lines(self) -> dict[str, int]:
        """Return DWARF declaration lines for all local variables in the frame.

        Runs a GDB Python script via ``-interpreter-exec console`` that walks
        every DWARF block within the current function's address range, then
        stores the result in a GDB convenience variable which is retrieved via
        ``-data-evaluate-expression``.

        Returns ``{var_name: decl_line}``.  Returns an empty dict on error.

        Why walk ALL blocks?
        --------------------
        ``frame.block()`` returns only the INNERMOST block at the current PC.
        Functions are commonly split into SIBLING blocks in DWARF — for
        example GCC puts variables declared after a ``goto`` label into a
        separate block.  Iterating only the current block (and its parents)
        misses those sibling blocks.

        The fix: starting from the function block's start address, jump from
        one block's end to the next to enumerate every sibling block.  At
        each PC, also walk UP the block chain to collect variables from outer
        (enclosing) blocks, using a visited set to avoid double-counting.

        The Python script (_DECL_LINES_SCRIPT) is base64-encoded so that the
        resulting MI command is a single safe ASCII string — no embedded
        newlines, quotes, or backslashes that would trip up the GDB MI parser.
        """
        py_cmd = f"import base64; exec(base64.b64decode('{_DECL_LINES_B64}').decode())"
        try:
            await self.mi_command_async(f'-interpreter-exec console "python {py_cmd}"')
        except RuntimeError as exc:
            _log.debug("get_decl_lines python step failed: %s", exc)
            return {}

        # Set both print limits to unlimited so the convenience variable value
        # is never truncated with "..." (which would drop remaining variable
        # entries).  GDB 14+ has a separate "print characters" setting that
        # controls string display independently of "print elements".
        # We cannot read the current values via $-gdb_setting() in an MI
        # data-evaluate-expression (the embedded quotes confuse the MI parser),
        # so we simply restore to the GDB defaults (200 / elements) afterwards.
        await self.mi_command_async("-gdb-set print elements unlimited")
        await self.mi_command_async("-gdb-set print characters unlimited")

        # Read back the convenience variable.
        try:
            raw = await self.eval_expr("$tgdb_decls")
        except RuntimeError as exc:
            _log.debug("get_decl_lines eval step failed: %s", exc)
            return {}
        finally:
            # Restore GDB defaults.  "elements 200" is the factory default;
            # "characters elements" (GDB 14+) means "follow print elements".
            try:
                await self.mi_command_async("-gdb-set print elements 200")
            except Exception:
                pass
            try:
                await self.mi_command_async("-gdb-set print characters elements")
            except Exception:
                pass  # older GDB without print characters — ignore

        # raw looks like: '"v:5,x:3"' (GDB wraps strings in double quotes)
        raw = raw.strip().strip('"')
        result: dict[str, int] = {}
        for part in raw.split(","):
            part = part.strip()
            if ":" not in part:
                continue
            name, _, line_str = part.partition(":")
            name = name.strip()
            line_str = line_str.strip()
            try:
                result[name] = int(line_str)
            except ValueError:
                pass

        _log.debug("get_decl_lines -> %s", result)
        return result

    async def eval_expr(self, expr: str) -> str:
        """Evaluate a GDB expression and return its value string."""
        result = await self.mi_command_async(f"-data-evaluate-expression {expr}")
        payload = result.get("payload") or {}
        return payload.get("value", "")

    async def var_evaluate_expression(self, varobj_name: str) -> str:
        """Return the current value string of a varobj without touching children.

        Unlike ``-var-update``, ``-var-evaluate-expression`` only calls the
        pretty-printer's ``to_string()`` method — it does NOT re-enumerate
        children.  For a dynamic varobj such as std::vector this means the
        summary (e.g. "std::vector of length 2, capacity 2") is refreshed
        quickly even when the container has many (or garbage-sized) children.
        """
        result = await self.mi_command_async(f"-var-evaluate-expression {varobj_name}")
        payload = result.get("payload") or {}
        value = payload.get("value", "")
        _log.debug("var_evaluate_expression %s -> %r", varobj_name, value)
        return value

    async def var_update(
        self, varobj_name: str = "*", timeout: float | None = 10.0
    ) -> list[dict]:
        """Update varobjs and return changed ones.

        *timeout* is forwarded to mi_command_async.  Pass a short value (e.g.
        1.5 s) for dynamic/pretty-printed varobjs whose pretty-printer may
        iterate a huge (possibly garbage) number of elements.
        """
        result = await self.mi_command_async(
            f"-var-update --all-values {varobj_name}", timeout=timeout
        )
        payload = result.get("payload") or {}
        changelist = payload.get("changelist", [])
        if not isinstance(changelist, list):
            changelist = []
        _log.debug("var_update %s -> %d changes", varobj_name, len(changelist))
        return changelist
