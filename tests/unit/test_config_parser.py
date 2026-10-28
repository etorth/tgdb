"""Config parser: :set, :highlight, :map, history reruns, :python, rc files."""

import asyncio
import io

import pytest

from tgdb.config import Config, ConfigParser
from tgdb.highlight_groups import HighlightGroups
from tgdb.key_mapper import KeyMapper


class FakeBar:
    """Minimal stand-in for CommandLineBar's history surface."""

    def __init__(self, history: list[str]):
        self._history = history


    def list_history(self) -> str:
        return "\n".join(self._history)


@pytest.fixture
def cp() -> ConfigParser:
    parser = ConfigParser(Config(), HighlightGroups(), KeyMapper())
    parser.set_cmdline_bar(FakeBar([]))
    return parser


def run(cp: ConfigParser, line: str, print_fn=None):
    return asyncio.run(cp.execute_async(line, print_fn=print_fn))


@pytest.mark.parametrize(
    ("line", "option", "value"),
    [
        ("set tabstop=4", "tabstop", 4),
        ("set ts=3", "tabstop", 3),
        (":set ic", "ignorecase", True),
        ("set hls", "hlsearch", True),
        ("set nowrapscan", "wrapscan", False),
        ("set sbbs=500", "scrollbackbuffersize", 500),
    ],
)
def test_set_options(cp, line, option, value):
    assert run(cp, line) is None
    assert getattr(cp.config, option) == value


@pytest.mark.parametrize("line", ["set tabstop 4", "set bogus", "set"])
def test_set_errors_are_reported(cp, line):
    assert run(cp, line)


def test_comment_lines_are_noops(cp):
    assert run(cp, "# set tabstop=2") is None
    assert cp.config.tabstop == 8


def test_highlight_command(cp):
    assert run(cp, "highlight Comment ctermfg=Blue ctermbg=NONE cterm=bold") is None
    group = cp.hl.get("Comment")
    assert (group.fg, group.bg, group.bold) == ("bright_blue", None, True)


def test_map_and_unmap(cp):
    assert run(cp, "map <F2> :next<CR>") is None
    expansion = cp.km.feed("tgdb", "f2")
    assert expansion[:5] == [":", "n", "e", "x", "t"]
    assert len(expansion) == 6
    assert run(cp, "unmap <F2>") is None
    assert cp.km.feed("tgdb", "f2") == ["f2"]


def test_bang_bang_skips_history_rerun_entries(cp):
    """Regression: ``!!`` is recorded before it runs and re-ran itself forever."""
    cp.set_cmdline_bar(FakeBar(["set tabstop=5", "# comment", "!!"]))
    assert run(cp, "!!") is None
    assert cp.config.tabstop == 5


def test_bang_n_refuses_rerun_entry(cp):
    cp.set_cmdline_bar(FakeBar(["set tabstop=5", "!1"]))
    assert "history command" in run(cp, "!2")
    assert run(cp, "!1") is None
    assert cp.config.tabstop == 5
    assert "out of range" in run(cp, "!9")


def test_python_namespace_persists(cp):
    out: list[str] = []
    assert run(cp, "python x = 21") is None
    assert run(cp, "python print(x * 2)", print_fn=out.append) is None
    assert "".join(out) == "42\n"


def test_python_heredoc_with_only_comments(cp):
    """Regression: a commented-out heredoc body was a SyntaxError."""
    assert run(cp, "python << EOF\n# nothing to do\nEOF") is None


def test_python_print_to_explicit_file(cp):
    """Regression: print(file=...) ignored ``file`` and went to the status bar."""
    sink = io.StringIO()
    cp.set_py_globals({"sink": sink})
    out: list[str] = []
    assert run(cp, 'python print("hi", file=sink)', print_fn=out.append) is None
    assert sink.getvalue() == "hi\n"
    assert out == []


def test_py_prefix_needs_word_boundary(cp):
    """Regression: ``Pyrun`` (case-insensitive) was executed as ``py run``."""
    result = run(cp, "Pyrun")
    assert result is None or "NameError" not in result


def test_run_script_reads_utf8_with_bom(cp, tmp_path):
    rc = tmp_path / "tgdbrc"
    rc.write_bytes("﻿# 司 comment\nset tabstop=3\n".encode("utf-8"))
    assert asyncio.run(cp.run_script_async(str(rc))) is None
    assert cp.config.tabstop == 3


def test_run_script_stops_at_first_error(cp, tmp_path):
    rc = tmp_path / "tgdbrc"
    rc.write_text("set tabstop=3\nset bogus\nset tabstop=6\n", encoding="utf-8")
    assert asyncio.run(cp.run_script_async(str(rc)))
    assert cp.config.tabstop == 3
