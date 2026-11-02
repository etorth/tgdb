"""Small pure helpers: XDG paths, disasm address matching."""

from pathlib import Path

import pytest

from tgdb.disasm_pane.pane import _same_addr
from tgdb.xdg_path import XDGPath


def test_xdg_uses_absolute_env(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert XDGPath.config_home() == tmp_path


@pytest.mark.parametrize("value", ["", "relative/dir"])
def test_xdg_ignores_empty_or_relative_env(monkeypatch, value):
    monkeypatch.setenv("XDG_STATE_HOME", value)
    assert XDGPath.state_home() == Path.home() / ".local/state"


def test_app_does_not_inherit_textual_quit_binding():
    """Ctrl+Q must reach the GDB pane (readline quoted-insert), as in cgdb."""
    from tgdb.app import TGDBApp

    assert "ctrl+q" not in TGDBApp._merged_bindings.key_to_bindings


@pytest.mark.parametrize(
    ("a", "b", "same"),
    [
        ("0x0000555555555149", "0x555555555149", True),
        ("0x10", "0x10", True),
        ("0x10", "0x11", False),
        ("", "", False),
        ("0x10", "", False),
        ("junk", "0x10", False),
    ],
)
def test_disasm_same_addr(a, b, same):
    """Regression: MI pads addresses, the Python helper does not."""
    assert _same_addr(a, b) is same
