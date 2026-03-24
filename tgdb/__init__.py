"""tgdb — Python reimplementation of cgdb.

The ``tgdb`` package doubles as the scripting namespace for :python commands.
Import it inside a :python block to access workspace and screen utilities::

    import tgdb
    print(tgdb.screen.size())
    tgdb.screen.split(pane=[], mode=tgdb.SplitMode.HORIZONTAL)
    tgdb.screen.get_pane([0]).attach(tgdb.Pane.SOURCE)
"""
from .tgdb_api import screen, SplitMode, Pane, PaneHandle, TGDBScreen

__all__ = ["screen", "SplitMode", "Pane", "PaneHandle", "TGDBScreen"]
