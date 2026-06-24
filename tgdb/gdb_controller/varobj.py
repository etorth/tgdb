"""
VarobjMixin — varobj-related async MI commands.

Provides var_create, var_list_children, var_delete, var_update, and
get_decl_lines.  Mixed into ``GDBController``.
"""

import logging

from .types import quote_mi_string
from .value_format import decode_utf8_octal_escapes

_log = logging.getLogger("tgdb.gdb_varobj")


class VarobjMixin:
    """Mixin providing varobj commands built on the controller MI helper.

    Expects the host class to provide ``mi_command_async(...)`` with the
    ``timeout`` and ``raise_on_error`` parameters exposed by
    ``GDBRequestMixin.mi_command_async``.
    """

    # -- varobj commands ---------------------------------------------------

    @staticmethod
    def _normalize_varobj_value(info: dict) -> dict:
        value = info.get("value")
        if isinstance(value, str):
            info["value"] = decode_utf8_octal_escapes(value)
        return info


    async def _eval_expr_raw(self, expr: str, *, timeout: float | None = 5.0) -> str:
        """Evaluate a GDB expression and return its raw value string.

        *timeout* is forwarded to ``mi_command_async``.  The default 5 s
        is kept for ad-hoc single-value evaluations.
        """
        result = await self.mi_command_async(
            f"-data-evaluate-expression {quote_mi_string(expr)}",
            timeout=timeout,
            raise_on_error=True,
        )
        payload = result.get("payload") or {}
        value = payload.get("value", "")
        if isinstance(value, str):
            return decode_utf8_octal_escapes(value)
        return ""


    async def var_create(self, expr: str, *, frame: str = "*") -> dict:
        """Create a varobj for *expr*.  Returns the MI result payload.

        Keys include ``name``, ``numchild``, ``value``, ``type``, etc.
        """
        result = await self.mi_command_async(
            f"-var-create - {frame} {quote_mi_string(expr)}",
            raise_on_error=True,
        )
        payload = result.get("payload") or {}
        if isinstance(payload, dict):
            payload = self._normalize_varobj_value(payload)
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
                f" {quote_mi_string(varobj_name)} {from_idx} {from_idx + limit}"
            )
        else:
            cmd = f"-var-list-children --all-values {quote_mi_string(varobj_name)}"
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
                        children.append(self._normalize_varobj_value(child))
        elif isinstance(children_raw, dict):
            child = children_raw.get("child", children_raw)
            if isinstance(child, dict):
                children.append(self._normalize_varobj_value(child))
        _log.debug(
            f"var_list_children {varobj_name} from={from_idx} "
            f"limit={limit} -> {len(children)} children has_more={has_more}"
        )
        return children, has_more


    async def var_delete(self, varobj_name: str) -> None:
        """Delete a varobj and its children."""
        _log.debug(f"var_delete {varobj_name}")
        try:
            await self.mi_command_async(
                f"-var-delete {quote_mi_string(varobj_name)}",
                raise_on_error=True,
            )
        except RuntimeError:
            pass


    async def get_decl_lines(self) -> dict[str, int]:
        """Stub kept for backward compatibility — always returns empty dict.

        Declaration-line filtering is now done inside the GDB-side collection
        functions in ``tgdb_pysetup.py``, which use ``frame.find_sal().line``
        to skip variables declared after the current executing line before
        the result even leaves GDB.
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
            f"-var-evaluate-expression {quote_mi_string(varobj_name)}",
            raise_on_error=True,
        )
        payload = result.get("payload") or {}
        value = payload.get("value", "")
        if isinstance(value, str):
            value = decode_utf8_octal_escapes(value)
        else:
            value = ""
        _log.debug(f"var_evaluate_expression {varobj_name} -> {value!r}")
        return value


    async def var_update(self, varobj_name: str = "*", timeout: float | None = 10.0) -> list[dict]:
        """Update varobjs and return changed ones.

        *timeout* is forwarded to mi_command_async.  Pass a short value (e.g.
        1.5 s) for dynamic/pretty-printed varobjs whose pretty-printer may
        iterate a huge (possibly garbage) number of elements.
        """
        target = "*" if varobj_name == "*" else quote_mi_string(varobj_name)
        result = await self.mi_command_async(
            f"-var-update --all-values {target}",
            timeout=timeout,
            raise_on_error=True,
        )
        payload = result.get("payload") or {}
        changelist = payload.get("changelist", [])
        if not isinstance(changelist, list):
            changelist = []
        for change in changelist:
            if isinstance(change, dict):
                self._normalize_varobj_value(change)
        _log.debug(f"var_update {varobj_name} -> {len(changelist)} changes")
        return changelist
