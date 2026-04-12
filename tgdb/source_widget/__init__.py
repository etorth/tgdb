"""
Public entry point for the source-widget package.

This package intentionally preserves the old ``tgdb.source_widget`` import
surface. External code should keep importing ``SourceView``, ``SourceFile``,
source-pane message types, and the source-data constants from here while the
package keeps its data, message, and rendering helpers private to
``source_widget/``.

``SourceView`` itself is the black-box widget entry point: construct it with the
shared highlight palette, then drive it by assigning source/selection state and
handling the semantic messages it emits.
"""

from .data import (
    BP_DISABLED,
    BP_ENABLED,
    BP_NONE,
    SourceFile,
    _LOGO_LINES,
    _TOKEN_GROUPS,
    _token_group,
)
from .messages import (
    AwaitMarkJump,
    AwaitMarkSet,
    GDBCommand,
    JumpGlobalMark,
    OpenFileDialog,
    OpenTTY,
    ResizeSource,
    SearchCancel,
    SearchCommit,
    SearchStart,
    SearchUpdate,
    ShowHelp,
    StatusMessage,
    ToggleBreakpoint,
    ToggleOrientation,
)
from .pane import SourceView

__all__ = [
    "AwaitMarkJump",
    "AwaitMarkSet",
    "BP_DISABLED",
    "BP_ENABLED",
    "BP_NONE",
    "GDBCommand",
    "JumpGlobalMark",
    "OpenFileDialog",
    "OpenTTY",
    "ResizeSource",
    "SearchCancel",
    "SearchCommit",
    "SearchStart",
    "SearchUpdate",
    "ShowHelp",
    "SourceFile",
    "SourceView",
    "StatusMessage",
    "ToggleBreakpoint",
    "ToggleOrientation",
    "_LOGO_LINES",
    "_TOKEN_GROUPS",
    "_token_group",
]
