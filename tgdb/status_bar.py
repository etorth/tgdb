"""
Dedicated bottom status bar for ':' commands, search prompts, and messages.

Normally renders as one row.  Expands vertically during:
- Heredoc multi-line :python << MARKER input (collection phase)
- Multi-line command output / error display (dismiss phase)
"""
from __future__ import annotations

import re

from textual.widget import Widget
from textual.message import Message
from textual import events
from rich.text import Text

from .highlight_groups import HighlightGroups

# Matches ":python << EOF" / ":py << MYMARKER" (heredoc trigger)
_HEREDOC_RE = re.compile(r'^(python|py)\s+<<\s+(\S+)\s*$', re.IGNORECASE)


def _pad_crop(text: str, w: int) -> str:
    """Return *text* truncated or space-padded to exactly *w* characters."""
    if len(text) >= w:
        return text[:w]
    return text + " " * (w - len(text))


class StatusBar(Widget):
    """Status bar at the bottom of the screen.

    Normally 1 row.  Expands to multiple rows while:
    - A heredoc :python << MARKER block is being typed
    - A multi-line result/error from a command is being displayed
    """

    DEFAULT_CSS = """
    StatusBar {
        height: 1;
        background: $primary-darken-2;
    }
    """

    def __init__(self, hl: HighlightGroups, **kwargs) -> None:
        super().__init__(**kwargs)
        self.hl = hl
        self._mode: str = "GDB"
        self._message: str = ""
        self._input_active: bool = False
        self._input_buf: str = ""
        self._search_active: bool = False
        self._search_buf: str = ""
        self._search_forward: bool = True
        self.can_focus = True

        # ── Multiline (heredoc) input state ──────────────────────────
        self._ml_active: bool = False
        self._ml_buf: list[str] = []    # lines collected so far (not yet terminated)
        self._ml_marker: str = ""       # e.g. "EOF"
        self._ml_cmd: str = ""          # e.g. "python"

        # ── Multiline message display state ──────────────────────────
        self._msg_lines: list[str] = []
        self._msg_has_more: bool = False    # True when output was truncated

    # ------------------------------------------------------------------
    # Height management
    # ------------------------------------------------------------------

    def _set_height(self, n: int) -> None:
        self.styles.height = max(1, n)

    # ------------------------------------------------------------------
    # State setters (called by app)
    # ------------------------------------------------------------------

    def set_mode(self, mode: str) -> None:
        self._mode = mode
        self.refresh()

    def show_message(self, msg: str) -> None:
        """Show a single-line status message (collapses any multiline display)."""
        self._message = msg
        self._msg_lines = []
        self._msg_has_more = False
        self._set_height(1)
        self.refresh()

    def show_multiline_message(self, msg: str) -> None:
        """Expand the status bar to display a multi-line message.

        At most half the screen height is used; excess lines are summarised.
        The bar stays expanded until the user presses Enter / Escape / any key.
        """
        try:
            max_rows = max(3, self.app.size.height // 2)
        except Exception:
            max_rows = 12

        all_lines = msg.splitlines()
        max_content = max_rows - 1    # reserve 1 row for the dismiss hint

        if len(all_lines) <= max_content:
            self._msg_lines = all_lines
            self._msg_has_more = False
            n = len(all_lines) + 1
        else:
            self._msg_lines = all_lines[:max_content]
            self._msg_has_more = True
            n = max_rows

        self._message = ""
        self._set_height(n)
        self.refresh()

    def dismiss_message(self) -> None:
        """Collapse the multi-line message and restore the bar to 1 row."""
        self._msg_lines = []
        self._msg_has_more = False
        self._message = ""
        self._set_height(1)
        self.refresh()

    def start_command(self) -> None:
        self._input_active = True
        self._search_active = False
        self._input_buf = ""
        self.refresh()

    def start_search(self, forward: bool) -> None:
        self._search_active = True
        self._search_forward = forward
        self._search_buf = ""
        self._input_active = False
        self.refresh()

    def update_search(self, pattern: str) -> None:
        self._search_buf = pattern
        self.refresh()

    def cancel_input(self) -> None:
        self._input_active = False
        self._search_active = False
        self._ml_active = False
        self._ml_buf = []
        self._message = ""
        self._msg_lines = []
        self._set_height(1)
        self.refresh()

    # ------------------------------------------------------------------
    # Key dispatch
    # ------------------------------------------------------------------

    def feed_key(self, key: str, char: str) -> bool:
        """Handle one keystroke.  Returns True if the key was consumed."""

        # ── Multiline message dismiss ─────────────────────────────────
        if self._msg_lines:
            if key in ("enter", "return", "escape"):
                self.dismiss_message()
                self.post_message(CommandCancel())
                return True
            # Other keys are not consumed here; the app will dismiss.
            return False

        # ── Heredoc continuation input ────────────────────────────────
        if self._ml_active:
            if key == "escape":
                self._cancel_multiline()
                self.post_message(CommandCancel())
                return True

            if key in ("enter", "return"):
                line = self._input_buf
                self._input_buf = ""
                if line.strip() == self._ml_marker:
                    # Terminator found — assemble and submit the block
                    code = "\n".join(self._ml_buf)
                    cmd = self._ml_cmd
                    self._cancel_multiline()
                    self.post_message(CommandSubmit(f"{cmd}\n{code}"))
                else:
                    self._ml_buf.append(line)
                    # header + collected + current-input row
                    self._set_height(2 + len(self._ml_buf))
                self.refresh()
                return True

            if key in ("backspace", "ctrl+h"):
                self._input_buf = self._input_buf[:-1]
                self.refresh()
                return True

            if char and char.isprintable():
                self._input_buf += char
                self.refresh()
                return True

            return False

        # ── Normal single-line command input ──────────────────────────
        if not self._input_active:
            return False

        if key == "escape":
            self._input_active = False
            self.post_message(CommandCancel())

        elif key in ("enter", "return"):
            cmd = self._input_buf
            self._input_active = False
            self._input_buf = ""
            self.refresh()
            m = _HEREDOC_RE.match(cmd)
            if m:
                self._start_multiline(m.group(1), m.group(2))
            else:
                self.post_message(CommandSubmit(cmd))

        elif key in ("backspace", "ctrl+h"):
            self._input_buf = self._input_buf[:-1]
            if not self._input_buf:
                self._input_active = False
                self.post_message(CommandCancel())
            self.refresh()

        elif char and char.isprintable():
            self._input_buf += char
            self.refresh()

        else:
            return False

        return True

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _start_multiline(self, cmd: str, marker: str) -> None:
        self._input_active = False
        self._ml_active = True
        self._ml_cmd = cmd.lower()
        self._ml_marker = marker
        self._ml_buf = []
        self._input_buf = ""
        self._set_height(2)     # header row + current-input row
        self.refresh()

    def _cancel_multiline(self) -> None:
        self._ml_active = False
        self._ml_buf = []
        self._ml_marker = ""
        self._ml_cmd = ""
        self._input_buf = ""
        self._set_height(1)
        self.refresh()

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def render(self) -> Text:
        w = max(10, self.size.width or 80)
        style = self.hl.style("StatusLine")

        if self._ml_active:
            return self._render_ml_input(w, style)

        if self._msg_lines:
            return self._render_msg(w, style)

        # ── Single-line modes ─────────────────────────────────────────
        if self._input_active:
            t = Text(_pad_crop(f":{self._input_buf}", w), no_wrap=True, overflow="crop")
            t.stylize(style)
            return t

        if self._search_active:
            pfx = "/" if self._search_forward else "?"
            t = Text(_pad_crop(f"{pfx}{self._search_buf}", w), no_wrap=True, overflow="crop")
            t.stylize(style)
            return t

        if self._message:
            t = Text(_pad_crop(self._message, w), style=style, no_wrap=True, overflow="crop")
            return t

        return Text(" " * w, style=style, no_wrap=True, overflow="crop")

    def _render_ml_input(self, w: int, style: str) -> Text:
        """Render the heredoc continuation prompt (multi-row)."""
        lines = []
        lines.append(_pad_crop(f":python << {self._ml_marker}", w))
        for ln in self._ml_buf:
            lines.append(_pad_crop(f"  {ln}", w))
        # Current input row — append a block cursor marker
        lines.append(_pad_crop(f"  {self._input_buf}\u258f", w))
        t = Text("\n".join(lines))
        t.stylize(style)
        return t

    def _render_msg(self, w: int, style: str) -> Text:
        """Render the multiline message display (multi-row)."""
        lines = [_pad_crop(ln, w) for ln in self._msg_lines]
        if self._msg_has_more:
            hint = "-- (output truncated) Press ENTER or any key --"
        else:
            hint = "-- Press ENTER or any key to continue --"
        lines.append(_pad_crop(hint, w))
        t = Text("\n".join(lines))
        t.stylize(style)
        return t

    # ------------------------------------------------------------------
    # Key handling when the widget has focus
    # ------------------------------------------------------------------

    def on_key(self, event: events.Key) -> None:
        if self.feed_key(event.key, event.character or ""):
            event.stop()


class CommandSubmit(Message):
    def __init__(self, command: str) -> None:
        super().__init__()
        self.command = command


class CommandCancel(Message):
    pass
