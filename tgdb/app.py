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

import asyncio

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
from .callbacks import CallbacksMixin
from .commands import CommandsMixin
from .core import AppCoreMixin
from .keys import KeyRoutingMixin
from .layout import LayoutMixin
from .workspace import PaneDescriptor
from .workspace_actions import WorkspaceMixin


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
    .screen--selection {
        background: yellow;
        color: black;
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
    #completion-popup {
        display: none;
    }
    #completion-popup.visible {
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
        rc_file: str | None = None,
        attach_pid: int | None = None,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self.hl = HighlightGroups()
        self.km = KeyMapper()
        # Per-mode timer that flushes the key-mapper buffer after
        # ``timeoutlen`` ms of idle.  Without this, a partial map
        # sequence (e.g. user typed ``g`` of ``gg``) sits in the
        # buffer indefinitely; vim's idle-timeout behaviour is
        # restored by re-arming the timer on every feed() that
        # leaves the buffer non-empty.
        self._km_flush_timer: dict[str, object | None] = {}
        self.cfg = Config()
        self.cp = ConfigParser(self.cfg, self.hl, self.km)
        self._initial_source_pending = bool(gdb_args) or (attach_pid is not None)
        self._attach_pid: int | None = attach_pid
        self._register_commands()

        # Wire the tgdb stdlib singleton to this app instance.
        # This makes ``import tgdb; tgdb.screen.split(...)`` work inside
        # :python blocks without exposing any internal tgdb classes.
        import tgdb as _tgdb_pkg

        _tgdb_pkg.screen._set_app(self)
        from .memory_pane import MemoryFormatter as _MF
        self.cp.set_py_globals({
            "app": self,
            "tgdb": _tgdb_pkg,
            "MemoryFormatter": _MF,
        })

        self._rc_file: str | None = (
            rc_file  # resolved in on_mount after app is ready
        )

        self.gdb = GDBController(gdb_path=gdb_path, args=gdb_args or [])
        self._gdb_task: asyncio.Task | None = None
        self._cmd_task: asyncio.Task | None = None  # running CommandLineBar command
        self._pending_replay_tokens: list[str] = []  # map tokens queued after async <CR>

        self._mode: str = "GDB_PROMPT"
        self._await_mark_jump: bool = False
        self._await_mark_set: bool = False
        self._file_dialog_pending: bool = False
        self._inf_tty_fd: int | None = None
        self._shutting_down: bool = False
        self._context_menu_target: Widget | None = None
        self._locals_context_node: object | None = None  # TreeNode | None
        self._source_view: SourceView | None = None
        self._gdb_widget: GDBWidget | None = None
        self._locals_pane: LocalVariablePane | None = None
        self._current_locals: list[LocalVariable] = []
        self._stack_pane: StackPane | None = None
        self._current_stack: list[Frame] = []
        self._thread_pane: ThreadPane | None = None
        self._current_threads: list[ThreadInfo] = []
        self._register_pane: RegisterPane | None = None
        self._current_registers: list[RegisterInfo] = []
        self._evaluate_pane: EvaluatePane | None = None
        self._memory_panes: list[MemoryPane] = []
        self._disasm_pane: DisasmPane | None = None
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
                lambda: self._memory_panes[-1] if self._memory_panes else None,
                multi_instance=True,
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
