"""
Data classes for GDB/MI structured records.

These types are used across the tgdb package to represent debugger state
(breakpoints, frames, local variables, threads, registers).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


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


@dataclass
class ThreadInfo:
    id: str = ""
    target_id: str = ""
    name: str = ""
    state: str = ""
    core: str = ""
    frame: Optional[Frame] = None
    is_current: bool = False


@dataclass
class RegisterInfo:
    number: int = 0
    name: str = ""
    value: str = ""
