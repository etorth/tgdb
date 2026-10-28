"""tgdb — Python reimplementation of cgdb.

The ``tgdb`` package doubles as the scripting namespace for :python commands.
Import it inside a :python block to access workspace and screen utilities::

    import tgdb
    print(tgdb.screen.size())
    await tgdb.screen.split(pane=[], mode=tgdb.SplitMode.HORIZONTAL)
    await tgdb.screen.get_pane([0]).attach(tgdb.Pane.SOURCE)

    await tgdb.debugger.execute("break main")
    await tgdb.debugger.execute("run")
    print(tgdb.debugger.locals())

``tgdb.screen`` manages the pane layout; ``tgdb.debugger`` drives GDB and
reads debugger state (see ``TGDBDebugger``).
"""

from .tgdb_api import Pane, PaneHandle, SplitMode, TGDBDebugger, TGDBScreen, debugger, screen

__all__ = ["screen", "debugger", "SplitMode", "Pane", "PaneHandle", "TGDBScreen", "TGDBDebugger"]
