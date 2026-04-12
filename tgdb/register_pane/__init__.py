"""
Public entry point for the register-pane package.

External code should import :class:`RegisterPane` from ``tgdb.register_pane``.
The caller creates the widget once, then publishes parsed register snapshots
through ``set_registers(...)``.
"""

from .pane import RegisterPane

__all__ = ["RegisterPane"]
