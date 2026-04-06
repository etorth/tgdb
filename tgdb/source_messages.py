"""Message classes emitted by the source view widget."""

from __future__ import annotations

from textual.message import Message


class ToggleBreakpoint(Message):
    def __init__(self, line: int, temporary: bool = False) -> None:
        super().__init__()
        self.line = line
        self.temporary = temporary


class OpenFileDialog(Message):
    pass


class AwaitMarkJump(Message):
    pass


class AwaitMarkSet(Message):
    pass


class JumpGlobalMark(Message):
    def __init__(self, path: str, line: int) -> None:
        super().__init__()
        self.path = path
        self.line = line


class SearchStart(Message):
    def __init__(self, forward: bool) -> None:
        super().__init__()
        self.forward = forward


class SearchUpdate(Message):
    def __init__(self, pattern: str) -> None:
        super().__init__()
        self.pattern = pattern


class SearchCommit(Message):
    def __init__(self, pattern: str) -> None:
        super().__init__()
        self.pattern = pattern


class SearchCancel(Message):
    pass


class StatusMessage(Message):
    def __init__(self, text: str) -> None:
        super().__init__()
        self.text = text


class ResizeSource(Message):
    """Request to resize the source/gdb split.

    rows=True : delta is ±1 row (cgdb '=' / '-')
    jump=True : delta is ±1 quarter-mark step (cgdb '+' / '_')
    """

    def __init__(
        self, delta: int, rows: bool = False, jump: bool = False, percent: bool = False
    ) -> None:
        super().__init__()
        self.delta = delta
        self.rows = rows
        self.jump = jump
        self.percent = percent  # legacy, kept for compatibility


class ToggleOrientation(Message):
    pass


class OpenTTY(Message):
    pass


class ShowHelp(Message):
    pass


class GDBCommand(Message):
    def __init__(self, cmd: str) -> None:
        super().__init__()
        self.cmd = cmd
