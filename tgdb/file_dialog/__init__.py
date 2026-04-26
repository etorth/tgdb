"""
Public entry point for the file-dialog package.

External code should import :class:`FileDialog` and the dialog message types
from ``tgdb.file_dialog``. The caller creates the widget once, publishes the
current source-file snapshot through ``files`` or ``open_pending()``, and then
treats the dialog as a black box that owns selection, search, and open/close
signaling.
"""

from .dialog import FileDialog, FileDialogClosed, FileSelected

__all__ = ["FileDialog", "FileDialogClosed", "FileSelected"]
