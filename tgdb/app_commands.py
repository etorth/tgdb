"""CommandsMixin — :command handlers extracted from TGDBApp."""

from __future__ import annotations

import asyncio
import logging
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, TYPE_CHECKING

from .source_widget import SourceFile

if TYPE_CHECKING:
    from .app import TGDBApp

_log = logging.getLogger("tgdb.app")


class CommandsMixin:
    """All :command handlers and their registration."""

    def _register_commands(self: TGDBApp) -> None:
        def gdb_cmd(c):
            def handler(args):
                self._send_gdb_cli(c)

            return handler

        cmds = {
            "bang": self._cmd_bang,
            "quit": self._cmd_quit,
            "q": self._cmd_quit,
            "help": self._cmd_help,
            "edit": self._cmd_edit,
            "e": self._cmd_edit,
            "focus": self._cmd_focus,
            "insert": self._cmd_insert,
            "noh": self._cmd_noh,
            "shell": self._cmd_shell,
            "sh": self._cmd_shell,
            "logo": self._cmd_logo,
            "syntax": self._cmd_syntax,
            "capturescreen": self._cmd_capturescreen,
            "cs": self._cmd_capturescreen,
            "continue": gdb_cmd("continue"),
            "c": gdb_cmd("continue"),
            "next": gdb_cmd("next"),
            "n": gdb_cmd("next"),
            "nexti": gdb_cmd("nexti"),
            "step": gdb_cmd("step"),
            "s": gdb_cmd("step"),
            "stepi": gdb_cmd("stepi"),
            "finish": gdb_cmd("finish"),
            "f": gdb_cmd("finish"),
            "run": gdb_cmd("run"),
            "r": gdb_cmd("run"),
            "start": gdb_cmd("start"),
            "kill": gdb_cmd("kill"),
            "k": gdb_cmd("kill"),
            "until": gdb_cmd("until"),
            "u": gdb_cmd("until"),
            "up": gdb_cmd("up"),
            "down": gdb_cmd("down"),
            "_goto_line": self._cmd_goto_line,
            # New features
            "signal": self._cmd_signal,
            "break-condition": self._cmd_break_condition,
            "bc": self._cmd_break_condition,
            "watch": self._cmd_watch,
            "unwatch": self._cmd_unwatch,
            "memory": self._cmd_memory,
            "disasm": self._cmd_disasm,
        }
        for name, fn in cmds.items():
            self.cp.register_handler(name, fn)

    def _cmd_quit(self: TGDBApp, _: list) -> None:
        self._save_history_to_disk()
        self.gdb.terminate()
        self.exit(0)

    def _cmd_goto_line(self: TGDBApp, args: list) -> Optional[str]:
        """Handle :N line-jump: positive=goto, :+N=scroll down, :-N=scroll up."""
        if not args:
            return None
        raw = args[0]
        try:
            n = int(raw)
        except ValueError:
            return f"Invalid line number: {raw!r}"
        src = self._get_source_view()
        if src is None:
            return None
        if raw.startswith("+"):
            src.scroll_down(n)
        elif raw.startswith("-"):
            src.scroll_up(-n)
        else:
            if n > 0:
                src.move_to(n)
            else:
                src.move_to(1)
        return None

    def _cmd_help(self: TGDBApp, _: list) -> None:
        self._show_help_in_source()

    def _cmd_logo(self: TGDBApp, _: list) -> None:
        src = self._get_source_view()
        if src is not None:
            src.show_logo()

    def _cmd_edit(self: TGDBApp, _: list) -> None:
        src = self._get_source_view()
        if src is not None and src.source_file:
            src.load_file(src.source_file.path)

    def _cmd_bang(self: TGDBApp, _: list) -> None:
        # cgdb registers :bang, but command_do_bang() is currently a no-op.
        pass

    def _cmd_insert(self: TGDBApp, _: list) -> None:
        self._switch_to_gdb()

    def _cmd_focus(self: TGDBApp, args: list) -> Optional[str]:
        if len(args) != 1:
            return "focus: requires tgdb or gdb"
        if args[0].lower() == "gdb":
            self._switch_to_gdb()
            return None
        if args[0].lower() == "tgdb":
            self._switch_to_tgdb()
            return None
        return "focus: requires cgdb or gdb"

    def _cmd_noh(self: TGDBApp, _: list) -> None:
        self.cfg.hlsearch = False
        src = self._get_source_view()
        if src is not None:
            src.hlsearch = False
            src.refresh()

    def _cmd_syntax(self: TGDBApp, args: list) -> None:
        """Mirror cgdb's :syntax [on|off|c|asm|…] command."""
        if args:
            value = args[0]
        else:
            value = ""
        if value:
            self.cfg.syntax = value.lower()
        # No args: cgdb prints info (TODO); we just refresh
        self._sync_config()

    def _cmd_shell(self: TGDBApp, args: list) -> Optional[str]:
        import os
        import shlex
        import subprocess

        try:
            with self.suspend():
                if args:
                    subprocess.call(shlex.join(args), shell=True)
                else:
                    subprocess.call([os.environ.get("SHELL", "/bin/sh")])
                try:
                    input("Hit ENTER to continue...")
                except EOFError:
                    pass
        except Exception as e:
            return str(e)
        self.refresh()
        return None

    def _cmd_capturescreen(self: TGDBApp, args: list) -> Optional[str]:
        """Save an SVG screenshot of the current screen.

        :capturescreen            — saves to tgdb-<nanosecond-timestamp>.svg
        :capturescreen myfile.svg — saves to myfile.svg
        """
        try:
            if args:
                filename = args[0]
            else:
                ns = time.time_ns()
                dt = datetime.fromtimestamp(ns // 1_000_000_000)
                nano = ns % 1_000_000_000
                ts = dt.strftime("%Y-%m-%d-%H-%M-%S-") + f"{nano:09d}"
                filename = f"tgdb-{ts}.svg"
            path = self.save_screenshot(filename=filename)
            self._show_status(f"Screenshot saved: {path}")
        except Exception as e:
            return str(e)
        return None

    def _send_gdb_cli(self: TGDBApp, cmd: str) -> None:
        _log.info("gdb cli: %r", cmd)
        if self.cfg.showdebugcommands:
            # Mirror cgdb showdebugcommands: echo the command into the GDB window
            gdb_w = self._get_gdb_widget()
            if gdb_w is not None:
                gdb_w.inject_text(f"(gdb) {cmd}\n")
        self.gdb.send_input(cmd + "\n")
        stripped = cmd.strip()
        if stripped:
            command_name = stripped.split(None, 1)[0].lower()
        else:
            command_name = ""
        if command_name in {"up", "down", "frame", "f", "select-frame", "thread"}:
            asyncio.get_running_loop().call_later(
                0.1,
                self._safe_request_location,
            )
        self._switch_to_gdb()

    def _safe_request_location(self: TGDBApp) -> None:
        """Safely request the current source location; swallow any error."""
        try:
            self.gdb.request_current_location(report_error=False)
        except Exception:
            pass

    def _show_help_in_source(self: TGDBApp) -> None:
        help_candidates = [
            Path("/usr/share/cgdb/cgdb.txt"),
            Path(sys.prefix) / "share" / "cgdb" / "cgdb.txt",
            Path(__file__).resolve().parents[1] / "doc" / "cgdb.txt",
        ]
        src = self._get_source_view()
        if src is None:
            self._show_status("No source pane available")
            return
        for candidate in help_candidates:
            if candidate.is_file():
                if src.load_file(str(candidate)):
                    src.exe_line = 0
                    src.move_to(1)
                    self._switch_to_tgdb()
                    return

        lines = [
            "tgdb — Python reimplementation of cgdb",
            "",
            "TGDB mode (source window, press ESC):",
            "  j/k      down/up lines           G/gg  bottom/top",
            "  Ctrl-f/b page down/up             H/M/L screen positions",
            "  Ctrl-d/u half page down/up",
            "  /        search forward           ?    search backward",
            "  n/N      next/prev match",
            "  Space    toggle breakpoint        t    temporary breakpoint",
            "  o        open file dialog",
            "  m[a-z]   set local mark           '[a-z]  jump to mark",
            "  ''       last jump location       '.  executing line",
            "  Ctrl-W   toggle split orientation",
            "  -/=      shrink/grow source pane  _/+  by 25%",
            "  F5=run  F6=continue  F7=finish  F8=next  F10=step",
            "  Rs/Rn/Rc/Rf  reverse-step/next/continue/finish (needs record)",
            "  i        switch to GDB mode",
            "  s        switch to GDB scroll mode",
            "  :        command-line (CMD) mode",
            "",
            "GDB mode (GDB console, press i):",
            "  ESC      back to TGDB mode        PageUp  scroll mode",
            "  All keys forwarded to GDB (readline, history, etc.)",
            "",
            "Scroll mode (PageUp in GDB window):",
            "  j/k/PageUp/Dn  scroll             G/gg  end/beginning",
            "  //?/n/N  search                   q/i/Enter  exit scroll",
            "",
            "Commands (type : in TGDB mode):",
            "  :set tabstop=4          :set hlsearch",
            "  :set winsplit=even      :set executinglinedisplay=longarrow",
            "  :set ecl=100 / expandchildlimit=100  (locals pane: items per page, 0=no limit)",
            "  :highlight Statement ctermfg=Yellow cterm=bold",
            "  :map <F8> :next<Enter>  :imap <F8> :next<Enter>",
            "  :break :continue :next :step :finish :run :quit",
            "  :shell [cmd]  run shell command    :capturescreen [file.svg]",
            "  :signal SIGNAME     send signal to inferior (e.g. :signal SIGTERM)",
            "  :bc N [expr]        set/clear breakpoint condition  (alias: :break-condition)",
            "  :watch expr         add expression to Watch pane",
            "  :unwatch N          remove watch expression by index",
            "  :memory addr [N]    inspect N bytes from addr in Memory pane",
            "  :disasm [on|off]    toggle inline disassembly in source pane",
            "  :set clipboardpath=/path/to/xclip  (sets pyperclip backend + PATH)",
        ]
        sf = SourceFile("<help>", lines)
        src.source_file = sf
        src.exe_line = 0
        src.move_to(1)
        self._switch_to_tgdb()

    # ------------------------------------------------------------------
    # New feature commands
    # ------------------------------------------------------------------

    def _cmd_signal(self: TGDBApp, args: list) -> Optional[str]:
        """Send a signal to the inferior: :signal SIGNAME (e.g. SIGTERM, 9)."""
        if not args:
            return "signal: requires a signal name or number (e.g. :signal SIGTERM)"
        self.gdb.send_signal(args[0])
        return None

    def _cmd_break_condition(self: TGDBApp, args: list) -> Optional[str]:
        """Set or clear a breakpoint condition.

        :bc N expr     — set condition ``expr`` on breakpoint N
        :bc N          — clear condition on breakpoint N
        """
        if not args:
            return "break-condition: requires breakpoint number (e.g. :bc 1 x>0)"
        try:
            num = int(args[0])
        except ValueError:
            return f"break-condition: {args[0]!r} is not a valid breakpoint number"
        condition = " ".join(args[1:])
        self.gdb.set_breakpoint_condition(num, condition)
        if condition:
            self._show_status(f"Breakpoint {num} condition: {condition}")
        else:
            self._show_status(f"Breakpoint {num} condition cleared")
        return None

    def _cmd_watch(self: TGDBApp, args: list) -> Optional[str]:
        """Add an expression to the Watch pane: :watch expr."""
        if not args:
            return "watch: requires an expression (e.g. :watch myvar)"
        expr = " ".join(args)
        watch_pane = getattr(self, "_watch_pane", None)
        if watch_pane is None:
            return "watch: Watch pane is not open (add it from context menu first)"
        watch_pane.add_expression(expr)
        self._show_status(f"Watching: {expr}")
        return None

    def _cmd_unwatch(self: TGDBApp, args: list) -> Optional[str]:
        """Remove a watch expression by 1-based index: :unwatch N."""
        if not args:
            return "unwatch: requires an index (e.g. :unwatch 1)"
        try:
            idx = int(args[0])
        except ValueError:
            return f"unwatch: {args[0]!r} is not a valid index"
        watch_pane = getattr(self, "_watch_pane", None)
        if watch_pane is None:
            return "unwatch: Watch pane is not open"
        removed = watch_pane.remove_expression(idx - 1)
        if removed:
            self._show_status(f"Removed watch: {removed}")
        else:
            self._show_status(f"unwatch: no expression at index {idx}")
        return None

    def _cmd_memory(self: TGDBApp, args: list) -> Optional[str]:
        """Inspect memory in the Memory pane: :memory addr [size].

        addr may be a hex literal (0x...), decimal, or a GDB expression.
        size defaults to 64 bytes.
        """
        if not args:
            return "memory: requires an address (e.g. :memory 0x7fffffffd000)"
        memory_pane = getattr(self, "_memory_pane", None)
        if memory_pane is None:
            return "memory: Memory pane is not open (add it from context menu first)"
        addr = args[0]
        try:
            size = int(args[1]) if len(args) > 1 else 64
        except ValueError:
            return f"memory: invalid size {args[1]!r}"
        memory_pane.set_address(addr, size)
        return None

    def _cmd_disasm(self: TGDBApp, args: list) -> Optional[str]:
        """Toggle inline disassembly in the source pane: :disasm [on|off]."""
        src = self._get_source_view()
        if src is None:
            return "disasm: no source pane available"
        if args:
            val = args[0].lower()
            if val in ("on", "1", "yes"):
                src.disasm_mode = True
            elif val in ("off", "0", "no"):
                src.disasm_mode = False
            else:
                return f"disasm: unknown value {args[0]!r} (use on/off)"
        else:
            src.disasm_mode = not getattr(src, "disasm_mode", False)
        state = "on" if getattr(src, "disasm_mode", False) else "off"
        self._show_status(f"Disassembly: {state}")
        return None
