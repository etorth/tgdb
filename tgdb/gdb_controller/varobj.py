"""
VarobjMixin — varobj-related async MI commands.

Provides var_create, var_list_children, var_delete, var_update, and the
get_locals helper that drives ``LocalVariablePane``. Mixed into
``GDBController``.
"""

from __future__ import annotations

import base64
import json
import logging

from .types import LocalVariable

_log = logging.getLogger("tgdb.gdb_varobj")


class VarobjMixin:
    """Mixin providing varobj commands built on the controller MI helper.

    Expects the host class to provide ``mi_command_async(...)`` with the
    ``timeout`` and ``raise_on_error`` parameters exposed by
    ``GDBRequestMixin.mi_command_async``.
    """

    # -- varobj commands ---------------------------------------------------

    async def _eval_expr_raw(self, expr: str) -> str:
        """Evaluate a GDB expression and return its raw value string."""
        result = await self.mi_command_async(
            f"-data-evaluate-expression {expr}",
            raise_on_error=True,
        )
        payload = result.get("payload") or {}
        return payload.get("value", "")

    async def var_create(self, expr: str, *, frame: str = "*") -> dict:
        """Create a varobj for *expr*.  Returns the MI result payload.

        Keys include ``name``, ``numchild``, ``value``, ``type``, etc.
        """
        result = await self.mi_command_async(
            f'-var-create - {frame} "{expr}"',
            raise_on_error=True,
        )
        payload = result.get("payload") or {}
        _log.debug(f"var_create expr={expr!r} -> name={payload.get('name')!r}")
        return payload


    async def var_list_children(self, varobj_name: str, from_idx: int = 0, limit: int = 0) -> tuple[list[dict], bool]:
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
        result = await self.mi_command_async(cmd, raise_on_error=True)
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
            f"var_list_children {varobj_name} from={from_idx} "
            f"limit={limit} -> {len(children)} children has_more={has_more}"
        )
        return children, has_more


    async def var_delete(self, varobj_name: str) -> None:
        """Delete a varobj and its children."""
        _log.debug(f"var_delete {varobj_name}")
        try:
            await self.mi_command_async(f"-var-delete {varobj_name}", raise_on_error=True)
        except RuntimeError:
            pass


    async def get_locals(self) -> list[dict]:
        """Fetch local variables from GDB via the ``$get_locals_b64()`` convenience function.

        The function is registered by ``tgdb_pysetup.py`` at GDB startup.
        It returns a base64-encoded JSON array of variable dicts which we
        decode here and return as ``list[dict]``.
        """
        await self.mi_command_async("-gdb-set print elements unlimited", raise_on_error=False)
        try:
            await self.mi_command_async("-gdb-set print characters unlimited", raise_on_error=True)
        except RuntimeError:
            pass  # older GDB without separate print characters setting

        try:
            raw = await self._eval_expr_raw("$get_locals_b64()")
        except RuntimeError as exc:
            _log.debug(f"get_locals failed: {exc}")
            return []
        finally:
            try:
                await self.mi_command_async("-gdb-set print elements 200", raise_on_error=False)
            except RuntimeError:
                pass
            try:
                await self.mi_command_async("-gdb-set print characters elements", raise_on_error=False)
            except RuntimeError:
                pass

        encoded = raw.strip().strip('"')
        if not encoded:
            return []

        try:
            locals_list = json.loads(base64.b64decode(encoded).decode("utf-8"))
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            _log.debug(f"get_locals decode failed: {exc}")
            return []

        if isinstance(locals_list, list):
            return locals_list

        _log.debug(f"get_locals: unexpected payload type {type(locals_list).__name__}")
        return []


    async def _publish_locals_async(self) -> None:
        """Fetch locals via get_locals() and publish them through on_locals().

        Called instead of ``request_current_frame_locals()`` so that
        ``-stack-list-variables`` is never sent.  The richer ``LocalVariable``
        objects produced here carry ``addr`` and ``is_shadowed`` directly from
        GDB Python, which lets ``LocalVariablePane`` build its binding keys
        without an extra ``&name`` evaluation round-trip.
        """
        try:
            dicts = await self.get_locals()
        except Exception as exc:
            _log.debug(f"_publish_locals_async failed: {exc}")
            self.on_locals([])
            return

        variables = [
            LocalVariable(
                name=d.get("name", ""),
                value=d.get("value", ""),
                type=d.get("type", ""),
                is_arg=bool(d.get("is_arg", False)),
                addr=d.get("addr", ""),
                is_shadowed=bool(d.get("is_shadowed", False)),
            )
            for d in dicts
            if d.get("name")
        ]
        _log.debug(f"_publish_locals_async: {len(variables)} variables")
        self.on_locals(variables)


    async def get_decl_lines(self) -> dict[str, int]:
        """Stub kept for backward compatibility — always returns empty dict.

        Declaration-line filtering is now done inside ``get_locals_b64()`` in
        ``tgdb_pysetup.py``, which uses ``frame.find_sal().line`` to skip
        variables declared after the current executing line before the result
        even leaves GDB.  The ``get_decl_lines`` callback is no longer wired
        into ``LocalVariablePane``; this method remains so callers that
        reference ``self.gdb.get_decl_lines`` do not get an AttributeError.
        """
        return {}


    async def var_evaluate_expression(self, varobj_name: str) -> str:
        """Return the current value string of a varobj without touching children.

        Unlike ``-var-update``, ``-var-evaluate-expression`` only calls the
        pretty-printer's ``to_string()`` method — it does NOT re-enumerate
        children.  For a dynamic varobj such as std::vector this means the
        summary (e.g. "std::vector of length 2, capacity 2") is refreshed
        quickly even when the container has many (or garbage-sized) children.
        """
        result = await self.mi_command_async(
            f"-var-evaluate-expression {varobj_name}",
            raise_on_error=True,
        )
        payload = result.get("payload") or {}
        value = payload.get("value", "")
        _log.debug(f"var_evaluate_expression {varobj_name} -> {value!r}")
        return value


    async def var_update(self, varobj_name: str = "*", timeout: float | None = 10.0) -> list[dict]:
        """Update varobjs and return changed ones.

        *timeout* is forwarded to mi_command_async.  Pass a short value (e.g.
        1.5 s) for dynamic/pretty-printed varobjs whose pretty-printer may
        iterate a huge (possibly garbage) number of elements.
        """
        result = await self.mi_command_async(
            f"-var-update --all-values {varobj_name}",
            timeout=timeout,
            raise_on_error=True,
        )
        payload = result.get("payload") or {}
        changelist = payload.get("changelist", [])
        if not isinstance(changelist, list):
            changelist = []
        _log.debug(f"var_update {varobj_name} -> {len(changelist)} changes")
        return changelist
