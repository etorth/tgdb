"""
Data classes for GDB/MI structured records.

These types are used across the tgdb package to represent debugger state
(breakpoints, frames, local variables, threads, registers).
"""

from dataclasses import dataclass


@dataclass
class Breakpoint:
    number: int
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
