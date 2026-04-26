"""State-transition helpers for ``CommandLineBar``."""

class CommandLineStateMixin:
    """Mixin providing prompt/message state management for ``CommandLineBar``."""

    def _set_height(self, n: int) -> None:
        self.styles.height = max(1, n)


    def _clear_multiline_message_state(self) -> None:
        self._msg_lines = []
        self._msg_scroll = 0
        self._msg_visible_rows = 0


    def _clear_message_state(self) -> None:
        self._message = ""
        self._clear_multiline_message_state()


    def _collapse_to_single_line(self) -> None:
        self._set_height(1)


    def set_mode(self, mode: str) -> None:
        self._mode = mode
        self.refresh()


    def show_message(self, msg: str) -> None:
        self._message = msg
        self._clear_multiline_message_state()
        self._collapse_to_single_line()
        self.refresh()


    def show_multiline_message(self, msg: str) -> None:
        try:
            max_rows = max(5, self.app.size.height // 2)
        except Exception:
            max_rows = 12

        all_lines = msg.splitlines()
        actual_height = min(len(all_lines) + 1, max_rows)
        actual_height = max(2, actual_height)

        self._msg_lines = all_lines
        self._msg_scroll = 0
        self._msg_visible_rows = actual_height - 1

        self._message = ""
        self._set_height(actual_height)
        self.refresh()


    def dismiss_message(self) -> None:
        self._clear_message_state()
        self._collapse_to_single_line()
        self.refresh()


    def start_command(self) -> None:
        self._clear_message_state()
        self._streaming_buf = ""
        self._input_active = True
        self._search_active = False
        self._input_buf = ""
        self._cursor_pos = 0
        self._history_idx = -1
        self._history_prefix = ""
        self._popup_active = False
        self._completions = []
        self._completion_idx = 0
        self._collapse_to_single_line()
        self.refresh()


    def start_search(self, forward: bool) -> None:
        self._search_active = True
        self._search_forward = forward
        self._search_buf = ""
        self._input_active = False
        self._message = ""
        self.refresh()


    def update_search(self, pattern: str) -> None:
        self._search_buf = pattern
        self.refresh()


    def cancel_input(self) -> None:
        self._input_active = False
        self._search_active = False
        self._ml_active = False
        self._ml_buf = []
        self._popup_active = False
        self._completions = []
        self._completion_idx = 0
        self._clear_message_state()
        self._collapse_to_single_line()
        self.refresh()
