"""
Public entry point for the configuration package.

External code should import :class:`Config`, :class:`ConfigParser`, and
``UserCommandDef`` from ``tgdb.config``. The caller constructs a mutable
``Config`` state object, wires ``ConfigParser`` to the live highlight/key-mapper
objects, and then treats the parser as the black-box dispatcher for rc-file and
command-line configuration commands.
"""

from .parser import ConfigParser
from .types import Config, UserCommandDef

__all__ = ["Config", "ConfigParser", "UserCommandDef"]
