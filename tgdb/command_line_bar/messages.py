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


class CompletionPopupShow(Message):
    """Request the floating completion popup to open with these candidates.

    ``anchor_col`` is the column (in the bar's local coordinate space) where
    the leftmost cell of the popup should be aligned. The app translates this
    to absolute screen coordinates and positions the popup above the bar.
    """

    def __init__(self, items: list[str], selected_idx: int, anchor_col: int) -> None:
        super().__init__()
        self.items = items
        self.selected_idx = selected_idx
        self.anchor_col = anchor_col


class CompletionPopupUpdate(Message):
    """Update the highlighted row of the open completion popup."""

    def __init__(self, selected_idx: int) -> None:
        super().__init__()
        self.selected_idx = selected_idx


class CompletionPopupHide(Message):
    """Request the floating completion popup to close."""

    pass

