"""
Public implementation of the tgdb application package.

``TGDBApp`` mirrors cgdb's interface.cpp + cgdb.cpp.

Global layout:
  ┌──────────────────────────┐
  │    Source / GDB area     │
  ├──────────────────────────┤
  │    command-line bar      │  1 line
  └──────────────────────────┘

The source pane itself reserves its bottom row for the current file path.

Modes: TGDB | GDB_PROMPT | GDB_SCROLL | CMD | ML_MESSAGE | FILEDLG
"""

from __future__ import annotations

import asyncio
from typing import Optional

from textual.app import App
from textual.widget import Widget

from .command_line_bar import CommandLineBar
from .config import Config, ConfigParser
from .context_menu import ContextMenu
from .disasm_pane import DisasmPane
from .evaluate_pane import EvaluatePane
from .file_dialog import FileDialog
from .gdb_controller import (
    GDBController,
    Frame,
    LocalVariable,
    RegisterInfo,
    ThreadInfo,
)
from .gdb_widget import GDBWidget
from .highlight_groups import HighlightGroups
from .key_mapper import KeyMapper
from .local_variable_pane import LocalVariablePane
from .memory_pane import MemoryPane
from .register_pane import RegisterPane
from .source_widget import SourceView
from .stack_pane import StackPane
from .thread_pane import ThreadPane
from .app_callbacks import CallbacksMixin
from .app_commands import CommandsMixin
from .app_core import AppCoreMixin
from .app_keys import KeyRoutingMixin
from .app_layout import LayoutMixin
from .workspace import PaneContainer, PaneDescriptor
from .app_workspace import WorkspaceMixin


class TGDBApp(
    AppCoreMixin,
    CommandsMixin,
    WorkspaceMixin,
    LayoutMixin,
    KeyRoutingMixin,
    CallbacksMixin,
    App,
):
    """tgdb — Python front-end for GDB, compatible with cgdb."""

    CSS = """
    Screen {
        layers: base dialog;
        layout: vertical;
    }
    #global-container {
        layer: base;
        layout: vertical;
        height: 1fr;
        width: 1fr;
    }
    #split-container {
        height: 1fr;
        width: 1fr;
    }
    #src-pane {
        width: 1fr;
        height: 1fr;
        min-height: 2;
        min-width: 4;
    }
    #cmdline {
        height: 1;
        width: 1fr;
    }
    #gdb-pane {
        width: 1fr;
        height: 1fr;
        min-height: 2;
        min-width: 4;
    }
    #context-menu {
        display: none;
    }
    #context-menu.visible {
        display: block;
    }
    #file-dlg {
        layer: dialog;
        width: 1fr;
        height: 1fr;
        display: none;
        background: $surface;
    }
    #file-dlg.visible {
        display: block;
    }
    """

    def __init__(
        self,
        gdb_path: str = "gdb",
        gdb_args: list[str] | None = None,
        rc_file: Optional[str] = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.hl = HighlightGroups()
        self.km = KeyMapper()
        self.cfg = Config()
        self.cp = ConfigParser(self.cfg, self.hl, self.km)
        self._initial_source_pending = bool(gdb_args)
        self._register_commands()

        # Wire the tgdb stdlib singleton to this app instance.
        # This makes ``import tgdb; tgdb.screen.split(...)`` work inside
        # :python blocks without exposing any internal tgdb classes.
        import tgdb as _tgdb_pkg

        _tgdb_pkg.screen._set_app(self)
        self.cp.set_py_globals({"app": self, "tgdb": _tgdb_pkg})

        self._rc_file: Optional[str] = (
            rc_file  # resolved in on_mount after app is ready
        )

        self.gdb = GDBController(gdb_path=gdb_path, args=gdb_args or [])
        self._gdb_task: Optional[asyncio.Task] = None
        self._cmd_task: Optional[asyncio.Task] = None  # running CommandLineBar command
        self._pending_replay_tokens: list[str] = []  # map tokens queued after async <CR>

        self._mode: str = "GDB_PROMPT"
        self._await_mark_jump: bool = False
        self._await_mark_set: bool = False
        self._split_ratio: float = 0.5
        self._cur_win_split: int = {
            "gdb_full": -2,
            "gdb_big": -1,
            "even": 0,
            "src_big": 1,
            "src_full": 2,
        }.get(self.cfg.winsplit.lower(), 0)
        self._window_shift: int = 0
        self._last_split_setting: str = ""
        self._last_orientation: str = ""
        self._preserve_window_shift_once: bool = False
        self._file_dialog_pending: bool = False
        self._inf_tty_fd: Optional[int] = None
        self._context_menu_target: Optional[Widget] = None
        self._locals_context_node: Optional[object] = None  # TreeNode | None
        self._source_view: Optional[SourceView] = None
        self._gdb_widget: Optional[GDBWidget] = None
        self._locals_pane: Optional[LocalVariablePane] = None
        self._current_locals: list[LocalVariable] = []
        self._stack_pane: Optional[StackPane] = None
        self._current_stack: list[Frame] = []
        self._thread_pane: Optional[ThreadPane] = None
        self._current_threads: list[ThreadInfo] = []
        self._register_pane: Optional[RegisterPane] = None
        self._current_registers: list[RegisterInfo] = []
        self._evaluate_pane: Optional[EvaluatePane] = None
        self._memory_pane: Optional[MemoryPane] = None
        self._disasm_pane: Optional[DisasmPane] = None
        self._in_map_replay: bool = False
        self._pane_descriptors: dict[str, PaneDescriptor] = {
            "source": PaneDescriptor(
                "Source", self._make_source_pane, lambda: self._source_view
            ),
            "gdb": PaneDescriptor("GDB", self._make_gdb_pane, lambda: self._gdb_widget),
            "locals": PaneDescriptor(
                "Locals",
                self._make_local_variable_pane,
                lambda: self._locals_pane,
                lambda: self.gdb.request_current_frame_locals(report_error=False),
            ),
            "registers": PaneDescriptor(
                "Registers",
                self._make_register_pane,
                lambda: self._register_pane,
                lambda: self.gdb.request_current_registers(report_error=False),
            ),
            "stack": PaneDescriptor(
                "Stack",
                self._make_stack_pane,
                lambda: self._stack_pane,
                lambda: self.gdb.request_current_stack_frames(report_error=False),
            ),
            "threads": PaneDescriptor(
                "Threads",
                self._make_thread_pane,
                lambda: self._thread_pane,
                lambda: self.gdb.request_current_threads(report_error=False),
            ),
            "evaluate": PaneDescriptor(
                "Evaluations",
                self._make_evaluate_pane,
                lambda: self._evaluate_pane,
            ),
            "memory": PaneDescriptor(
                "Memory",
                self._make_memory_pane,
                lambda: self._memory_pane,
            ),
            "disasm": PaneDescriptor(
                "Disasm",
                self._make_disasm_pane,
                lambda: self._disasm_pane,
            ),
        }
        self._add_menu_order: tuple[str, ...] = (
            "source",
            "gdb",
            "locals",
            "registers",
            "threads",
            "stack",
            "evaluate",
            "disasm",
            "memory",
        )
