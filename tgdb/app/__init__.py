"""
Public entry point for the application package.

External code should import :class:`TGDBApp` from ``tgdb.app`` and treat it as
the black-box Textual application object. The package also re-exports the
workspace layout primitives used by ``tgdb.screen`` so the application and its
workspace tree stay self-contained under one package boundary.
"""

from .main import TGDBApp
from .workspace import (
    DragResize,
    EmptyPane,
    PaneContainer,
    PaneDescriptor,
    Splitter,
    TitleBarResized,
)

__all__ = [
    "DragResize",
    "EmptyPane",
    "PaneContainer",
    "PaneDescriptor",
    "Splitter",
    "TGDBApp",
    "TitleBarResized",
]
