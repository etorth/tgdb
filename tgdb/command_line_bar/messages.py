"""Message types emitted by ``CommandLineBar``."""

from __future__ import annotations

from textual.message import Message


class CommandSubmit(Message):
    """Request execution of a completed command-line entry."""

    def __init__(self, command: str, *, history_text: str = "") -> None:
        super().__init__()
        self.command = command
        self.history_text = history_text


class CommandCancel(Message):
    """Signal that command-line input was cancelled."""

    pass


class MessageDismissed(Message):
    """Signal that the multiline message display was dismissed."""

    pass
