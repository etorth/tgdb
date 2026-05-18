"""
Data classes for GDB/MI structured records.

These types are used across the tgdb package to represent debugger state
(breakpoints, frames, local variables, threads, registers).
"""

import asyncio
import re
from dataclasses import dataclass


_ADDR_HEX_RE = re.compile(r"0x[0-9a-fA-F]+")


def quote_mi_string(s: str) -> str:
    """Escape and double-quote *s* for inclusion in an MI command argument.

    GDB/MI uses C-string quoting for free-form arguments (the expression
    of ``-data-evaluate-expression``, ``-var-create``, etc.): backslashes
    escape themselves and double quotes.  Interpolating raw text — even
    GDB-synthesised text such as a type string — produces a malformed MI
    command line whenever the value contains ``"`` or ``\\`` or a literal
    newline, which silently desyncs the request/response correlation
    because the parser sees what looks like a different record.
    """
    escaped = s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


def normalize_addr(addr: str) -> str:
    """Reduce an address string to a canonical ``0x...`` token.

    GDB emits stack addresses in two formats depending on the capture path:

    - ``str(gdb.Value.address)`` from the embedded Python helper produces
      a bare ``"0x7fffffffd123"`` (or, on some GDB versions / print
      settings, a type-prefixed ``"(int *) 0x7fffffffd123"``).
    - MI ``-data-evaluate-expression "&name"`` always returns the
      type-prefixed form ``"(int *) 0x7fffffffd123"``.

    Both are canonicalised here to the bare hex token so that BindingKeys
    built by either capture path compare equal across refreshes.  Without
    this, falling back from the fast (Python-helper) path to the slow
    (MI ``&name``) path mid-session makes every variable look "removed
    and re-added" because the addr field changes format, dropping
    expansion state and forcing a full tree rebuild.

    Empty strings and the sentinels ``"register"`` / ``"unknown"`` pass
    through unchanged.
    """
    if not addr or addr in ("register", "unknown"):
        return addr
    match = _ADDR_HEX_RE.search(addr)
    if match:
        return match.group(0).lower()
    return addr


@dataclass
class Breakpoint:
    # ``number`` is the GDB breakpoint id as a string so it can hold
    # both parent ids ("3") and child-location ids ("3.1", "3.2") for
    # multi-location breakpoints (e.g. ``b template_fn`` matching N
    # instantiations).  The flatten model keeps one Breakpoint entry
    # per addressable location so the source pane can mark every
    # location's ``(file, line)`` without a separate parent/child
    # tree.  Operations like delete must use the parent id —
    # ``number.partition('.')[0]`` — since GDB cannot delete a single
    # child location.
    number: str
    file: str = ""
    fullname: str = ""
    line: int = 0
    addr: str = ""
    enabled: bool = True
    temporary: bool = False
    condition: str = ""


@dataclass
class Frame:
    level: int = 0
    file: str = ""
    fullname: str = ""
    line: int = 0
    func: str = ""
    addr: str = ""


@dataclass
class LocalVariable:
    name: str = ""
    value: str = ""
    type: str = ""
    is_arg: bool = False
    addr: str = ""          # stack address from GDB Python (empty on fallback path)
    is_shadowed: bool = False  # True when an inner scope has a same-named variable
    is_reference: bool = False  # True for lvalue & and rvalue && reference types
    line: int = 0           # declaration line from GDB DWARF (0 = unknown)
    depth: int = 0          # block depth from get_locals_b64(): 0 = innermost


@dataclass
class ThreadInfo:
    id: str = ""
    target_id: str = ""
    name: str = ""
    state: str = ""
    core: str = ""
    frame: Frame | None = None
    is_current: bool = False


@dataclass
class RegisterInfo:
    number: int = 0
    name: str = ""
    value: str = ""


@dataclass
class PendingEntry:
    """Bookkeeping for an in-flight MI command awaited by ``mi_command_async``.

    ``future``
        The asyncio Future that the caller awaits.
    ``expect_socket``
        True for convenience function calls whose real payload arrives
        through the AF_UNIX socketpair.  The Future is only resolved
        when *both* the MI response and the socket data have arrived.
    ``mi_response``
        Set by ``_handle_result`` when the MI ``^done`` response arrives.
        ``None`` until then.
    ``socket_response``
        Set by ``_try_resolve_sock_pending`` when socket data with this
        token arrives.  ``None`` until then.
    """

    future: asyncio.Future
    expect_socket: bool = False
    mi_response: dict | None = None
    socket_response: object | None = None
