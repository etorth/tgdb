"""
Public entry point for the command-line-bar package.

External code should import :class:`CommandLineBar` and its semantic message
types from ``tgdb.command_line_bar``. Once constructed, the app pushes
prompt/message/task state through the widget's public methods and handles the
messages it emits.
"""

from .bar import CommandLineBar
from .messages import CommandCancel, CommandSubmit, MessageDismissed

__all__ = [
    "CommandCancel",
    "CommandLineBar",
    "CommandSubmit",
    "MessageDismissed",
]
