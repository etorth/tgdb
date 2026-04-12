"""
Public entry point for the evaluate-pane package.

External code should import :class:`EvaluatePane` from ``tgdb.evaluate_pane``.
The caller creates the widget, injects one async evaluation callback through
``set_eval_fn(...)``, and mutates the watch list through the pane's public
methods.
"""

from .pane import EvaluatePane

__all__ = ["EvaluatePane"]
