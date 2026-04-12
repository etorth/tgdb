"""Async task helpers for ``CommandLineBar``."""

from __future__ import annotations

from typing import Callable


class CommandLineTaskMixin:
    """Mixin providing task-locking and streaming-output behavior."""

    def lock_for_task(self) -> int:
        self._task_gen += 1
        self._task_running = True
        self._streaming_buf = ""
        self._collected_lines = []
        self._input_active = False
        self._clear_message_state()
        self._collapse_to_single_line()
        self.refresh()
        return self._task_gen


    def append_output(self, chunk: str, *, task_gen: int = 0) -> None:
        if not chunk:
            return

        is_current = task_gen == self._task_gen
        if is_current:
            raw = chunk.rstrip("\n")
            if raw:
                self._collected_lines.extend(raw.split("\n"))

        if self._task_running:
            self._streaming_buf = chunk
            display_lines = chunk.rstrip("\n").split("\n")
            self._set_height(max(1, len(display_lines)))
            self.refresh()
        elif self._can_show_async_print():
            self._streaming_buf = chunk
            display_lines = chunk.rstrip("\n").split("\n")
            self._set_height(max(1, len(display_lines)))
            self.refresh()


    def _can_show_async_print(self) -> bool:
        if self._input_active or self._ml_active:
            return False
        if self._msg_lines:
            return False
        return True


    def get_collected_output(self) -> list[str]:
        lines = list(self._collected_lines)
        self._collected_lines = []
        return lines


    def finish_task(self) -> None:
        self._task_running = False
        self._streaming_buf = ""
        self._collapse_to_single_line()
        self.refresh()


    def set_completion_provider(self, provider: Callable) -> None:
        self._completion_provider = provider
