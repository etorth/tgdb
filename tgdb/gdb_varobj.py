"""
VarobjMixin — varobj-related async MI commands.

Provides var_create, var_list_children, var_delete, var_update, and the
underlying mi_command_async helper.  Mixed into GDBController.
"""

from __future__ import annotations

import asyncio
import logging
import os

_log = logging.getLogger("tgdb.gdb_varobj")


class VarobjMixin:
    """Mixin providing varobj commands and the async MI command helper.

    Expects the host class to have:
        _mi_master_fd : int
        _token        : int
        _pending      : dict[int, asyncio.Future]
        _request_meta : dict[int, dict[str, object]]
    """

    # -- async MI command helper -------------------------------------------

    async def mi_command_async(
        self, cmd: str, timeout: float | None = 10.0
    ) -> dict:
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
            varobj_name, from_idx, limit, len(children), has_more,
        )
        return children, has_more

    async def var_delete(self, varobj_name: str) -> None:
        """Delete a varobj and its children."""
        _log.debug("var_delete %s", varobj_name)
        try:
            await self.mi_command_async(f"-var-delete {varobj_name}")
        except RuntimeError:
            pass

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
        result = await self.mi_command_async(
            f"-var-evaluate-expression {varobj_name}"
        )
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
        changelist = changelist if isinstance(changelist, list) else []
        _log.debug("var_update %s -> %d changes", varobj_name, len(changelist))
        return changelist
