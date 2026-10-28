"""GDB/MI record parser."""

import subprocess
import sys
from pathlib import Path

import pytest

from tgdb.gdb_controller.miparser import GDBMIParser

REPO = Path(__file__).resolve().parents[2]
parse = GDBMIParser.parse_response


def test_result_record_with_token():
    rec = parse('12^done,value="42"')
    assert rec["type"] == "result"
    assert rec["message"] == "done"
    assert rec["token"] == 12
    assert rec["payload"] == {"value": "42"}


def test_error_record():
    rec = parse('7^error,msg="No symbol \\"x\\" in current context."')
    assert rec["message"] == "error"
    assert rec["payload"]["msg"] == 'No symbol "x" in current context.'


def test_nested_tuples_and_lists():
    rec = parse(
        '^done,stack=[frame={level="0",func="f"},frame={level="1",func="main"}],'
        'bkpt={number="1",locations=[{number="1.1"},{number="1.2"}]}'
    )
    payload = rec["payload"]
    assert [f["func"] for f in payload["stack"]] == ["f", "main"]
    assert [loc["number"] for loc in payload["bkpt"]["locations"]] == ["1.1", "1.2"]


def test_async_stopped_record():
    rec = parse('*stopped,reason="breakpoint-hit",frame={func="main",line="7"}')
    assert rec["type"] == "notify"
    assert rec["message"] == "stopped"
    assert rec["payload"]["frame"]["line"] == "7"


def test_console_stream_unescapes():
    rec = parse('~"a\\tb\\n"')
    assert rec["type"] == "console"
    assert rec["payload"] == "a\tb\n"


def test_empty_list_and_tuple():
    rec = parse("^done,threads=[],frame={}")
    assert rec["payload"]["threads"] == []
    assert rec["payload"]["frame"] == {}


def test_inferior_output_is_not_a_record():
    rec = parse("hello from the program")
    assert rec["type"] == "output"
    assert rec["payload"] == "hello from the program"


@pytest.mark.parametrize(
    "text",
    [
        "^done,a",
        "^done,a=",
        "^done,a=[",
        "^done,a=1",
        '^done,a=["x"',
        "^done,a={b=",
        "3*4,5",
        "=a,b",
    ],
)
def test_truncated_records_terminate(text):
    """Regression: a truncated record used to loop forever (100% CPU hang).

    Runs in a subprocess so a regression fails fast instead of leaving a
    spinning thread that starves the rest of the test run.
    """
    code = (
        "import sys\n"
        "from tgdb.gdb_controller.miparser import GDBMIParser\n"
        "try:\n"
        "    GDBMIParser.parse_response(sys.argv[1])\n"
        "except Exception:\n"
        "    pass\n"
    )
    try:
        subprocess.run([sys.executable, "-c", code, text], cwd=REPO, timeout=10, check=True)
    except subprocess.TimeoutExpired:
        pytest.fail(f"parser did not terminate on {text!r}")
