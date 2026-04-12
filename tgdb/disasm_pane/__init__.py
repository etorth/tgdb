"""
Public entry point for the disassembly-pane package.

External code should import :class:`DisasmPane` from ``tgdb.disasm_pane``. The
caller creates the widget, injects one async disassembly callback through
``set_disasm_fn(...)``, and then either pushes parsed lines directly or asks
the pane to refresh itself from a source location.
"""

from .pane import DisasmLine, DisasmPane

__all__ = ["DisasmLine", "DisasmPane"]
