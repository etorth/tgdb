"""
VarobjMixin — varobj-related async MI commands.

Provides var_create, var_list_children, var_delete, var_update, and the
underlying mi_command_async helper.  Mixed into GDBController.
"""

from __future__ import annotations

import asyncio
import os


class VarobjMixin:
    """Mixin providing varobj commands and the async MI command helper.

    Expects the host class to have:
        _mi_master_fd : int
        _token        : int
        _pending      : dict[int, asyncio.Future]
        _request_meta : dict[int, dict[str, object]]
    """

    # -- async MI command helper -------------------------------------------

    async def mi_command_async(self, cmd: str) -> dict:
        """Send an MI command and await its result.

        Returns ``{"message": str, "payload": dict|None}``.
        Raises ``RuntimeError`` on send failure or ``^error`` response.
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
            raise RuntimeError(f"MI write failed: {e}") from e

        result = await fut
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
        return result.get("payload") or {}

    async def var_list_children(self, varobj_name: str) -> list[dict]:
        """List children of *varobj_name*.  Returns a list of child dicts.

        Each child dict has keys: ``name``, ``exp``, ``numchild``, ``value``,
        ``type``, etc.
        """
        result = await self.mi_command_async(f"-var-list-children --all-values {varobj_name}")
        payload = result.get("payload") or {}
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
        return children

    async def var_delete(self, varobj_name: str) -> None:
        """Delete a varobj and its children."""
        try:
            await self.mi_command_async(f"-var-delete {varobj_name}")
        except RuntimeError:
            pass

    async def var_update(self, varobj_name: str = "*") -> list[dict]:
        """Update varobjs and return changed ones."""
        result = await self.mi_command_async(f"-var-update --all-values {varobj_name}")
        payload = result.get("payload") or {}
        changelist = payload.get("changelist", [])
        return changelist if isinstance(changelist, list) else []
