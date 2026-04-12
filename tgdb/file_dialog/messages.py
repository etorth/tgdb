"""Messages emitted by the file dialog widget."""

from __future__ import annotations

from textual.message import Message


class FileSelected(Message):
    """Request that tgdb open the selected source file."""

    def __init__(self, path: str) -> None:
        super().__init__()
        self.path = path


class FileDialogClosed(Message):
    """Signal that the file dialog should close without opening a file."""

    pass
