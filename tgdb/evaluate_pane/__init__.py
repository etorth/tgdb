"""
Public entry point for the evaluate-pane package.

External code should import :class:`EvaluatePane` from ``tgdb.evaluate_pane``.
The caller creates the widget, injects the varobj callbacks through
``set_var_callbacks(...)``, and mutates the watch list through the pane's
public methods.
"""

from .pane import EvaluatePane

__all__ = ["EvaluatePane"]
