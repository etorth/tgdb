"""Backward-compatibility shim — StatusBar has been renamed to CommandLineBar.

Import from command_line_bar instead.
"""
from .command_line_bar import CommandLineBar as StatusBar, CommandSubmit, CommandCancel

__all__ = ["StatusBar", "CommandSubmit", "CommandCancel"]
