"""
GDB/MI output parser — extracted from pygdbmi.

This module is a self-contained extraction of the MI response parsing logic
from the **pygdbmi** project by Chad Smith, available at:

    https://github.com/cs01/pygdbmi

pygdbmi is released under the MIT License:

    Copyright (c) 2016 Chad Smith <grassfedcode <at> gmail.com>

    Permission is hereby granted, free of charge, to any person obtaining
    a copy of this software and associated documentation files (the
    "Software"), to deal in the Software without restriction, including
    without limitation the rights to use, copy, modify, merge, publish,
    distribute, sublicense, and/or sell copies of the Software, and to
    permit persons to whom the Software is furnished to do so, subject
    to the following conditions:

    The above copyright notice and this permission notice shall be
    included in all copies or substantial portions of the Software.

    THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
    EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
    MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
    NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS
    BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN
    ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
    CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
    SOFTWARE.

The following code is adapted from:
  - pygdbmi/gdbmiparser.py   — parse_response() and internal helpers
  - pygdbmi/StringStream.py  — StringStream class
  - pygdbmi/gdbescapes.py    — unescape / advance_past_string_with_gdb_escapes

All three modules are merged here into a single file so that tgdb does not
need pygdbmi as a runtime dependency.  The logic is kept faithful to the
original; only import paths and minor formatting have been adjusted.
"""

from __future__ import annotations

import functools
import re
from typing import Any, Callable, Dict, Iterator, List, Match, Optional, Pattern, Tuple, Union


# =========================================================================
# String escape handling (from pygdbmi/gdbescapes.py)
# =========================================================================

# Regular expression matching both escapes and unescaped quotes in GDB MI
# escaped strings.
_ESCAPES_RE = re.compile(
    r"""
    (?P<before>.*?)
    (
        (
            \\
            (
                (?P<escaped_octal>
                    [0-7]{3}
                    (\\[0-7]{3})*
                )
                |
                (?P<escaped_char>.)
            )
        )
        |
        (?P<unescaped_quote>")
    )
    """,
    flags=re.VERBOSE,
)

_NON_OCTAL_ESCAPES = {
    "'": "'",
    "\\": "\\",
    "a": "\a",
    "b": "\b",
    "e": "\033",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
    '"': '"',
}


def _split_n_chars(s: str, n: int) -> Iterator[str]:
    """Iterate over *s* in chunks of *n* characters."""
    for i in range(0, len(s), n):
        yield s[i : i + n]


def _unescape_internal(
    escaped_str: str, *, expect_closing_quote: bool, start: int = 0
) -> Tuple[str, int]:
    """Core unescape logic for GDB MI strings.

    MI-mode escapes are similar to standard Python escapes but:
    * ``\\e`` is a valid escape (ESC).
    * ``\\NNN`` escapes use octal format.
    """
    unmatched_start_index = start
    found_closing_quote = False
    unescaped_parts: list[str] = []

    for match in _ESCAPES_RE.finditer(escaped_str, pos=start):
        unescaped_parts.append(match["before"])

        escaped_octal = match["escaped_octal"]
        escaped_char = match["escaped_char"]
        unescaped_quote = match["unescaped_quote"]

        _, unmatched_start_index = match.span()

        if escaped_octal is not None:
            octal_sequence_bytes = bytearray()
            for octal_number in _split_n_chars(escaped_octal.replace("\\", ""), 3):
                try:
                    octal_sequence_bytes.append(int(octal_number, base=8))
                except ValueError as exc:
                    raise ValueError(
                        f"Invalid octal number {octal_number!r} in {escaped_str!r}"
                    ) from exc
            try:
                replaced = octal_sequence_bytes.decode("utf-8")
            except UnicodeDecodeError:
                replaced = f"\\{escaped_octal}"

        elif escaped_char is not None:
            try:
                replaced = _NON_OCTAL_ESCAPES[escaped_char]
            except KeyError as exc:
                raise ValueError(
                    f"Invalid escape character {escaped_char!r} in {escaped_str!r}"
                ) from exc

        elif unescaped_quote:
            if not expect_closing_quote:
                raise ValueError(f"Unescaped quote found in {escaped_str!r}")
            found_closing_quote = True
            break

        else:
            raise AssertionError(
                f"Unreachable for string {escaped_str!r}"
            )

        unescaped_parts.append(replaced)

    if not found_closing_quote:
        if expect_closing_quote:
            raise ValueError(f"Missing closing quote in {escaped_str!r}")
        unescaped_parts.append(escaped_str[unmatched_start_index:])
        unmatched_start_index = -1

    return "".join(unescaped_parts), unmatched_start_index


def _unescape(escaped_str: str) -> str:
    """Unescape a GDB MI escaped string (without surrounding quotes)."""
    unescaped, _ = _unescape_internal(escaped_str, expect_closing_quote=False)
    return unescaped


def _advance_past_string_with_gdb_escapes(
    escaped_str: str, *, start: int = 0
) -> Tuple[str, int]:
    """Unescape a GDB MI string and find the closing double quote.

    Returns ``(unescaped_string, index_after_closing_quote)``.
    """
    return _unescape_internal(escaped_str, expect_closing_quote=True, start=start)


# =========================================================================
# StringStream (from pygdbmi/StringStream.py)
# =========================================================================

class _StringStream:
    """Lightweight mutable-index wrapper around a string."""

    def __init__(self, raw_text: str) -> None:
        self.raw_text = raw_text
        self.index = 0
        self.len = len(raw_text)

    def read(self, count: int) -> str:
        new_index = self.index + count
        if new_index > self.len:
            buf = self.raw_text[self.index:]
        else:
            buf = self.raw_text[self.index:new_index]
        self.index = new_index
        return buf

    def seek(self, offset: int) -> None:
        self.index = self.index + offset

    def advance_past_chars(self, chars: List[str]) -> str:
        start_index = self.index
        while True:
            current_char = self.raw_text[self.index]
            self.index += 1
            if current_char in chars:
                break
            elif self.index == self.len:
                break
        return self.raw_text[start_index : self.index - 1]

    def advance_past_string_with_gdb_escapes(self) -> str:
        assert self.index > 0 and self.raw_text[self.index - 1] == '"'
        unescaped_str, self.index = _advance_past_string_with_gdb_escapes(
            self.raw_text, start=self.index
        )
        return unescaped_str


# =========================================================================
# MI value/dict/array parsers (from pygdbmi/gdbmiparser.py)
# =========================================================================

_WHITESPACE = [" ", "\t", "\r", "\n"]

_GDB_MI_CHAR_DICT_START = "{"
_GDB_MI_CHAR_ARRAY_START = "["
_GDB_MI_CHAR_STRING_START = '"'
_GDB_MI_VALUE_START_CHARS = [
    _GDB_MI_CHAR_DICT_START,
    _GDB_MI_CHAR_ARRAY_START,
    _GDB_MI_CHAR_STRING_START,
]


def _parse_dict(stream: _StringStream) -> Dict:
    """Parse a GDB MI dictionary (``{key=val,...}``)."""
    obj: Dict[str, Union[str, list, dict]] = {}

    while True:
        c = stream.read(1)
        if c in _WHITESPACE:
            pass
        elif c in ["{", ","]:
            pass
        elif c in ["}", ""]:
            break
        else:
            stream.seek(-1)
            key, val = _parse_key_val(stream)
            if key in obj:
                # GDB bug workaround: duplicate keys are coalesced into a list.
                if isinstance(obj[key], list):
                    obj[key].append(val)  # type: ignore
                else:
                    obj[key] = [obj[key], val]
            else:
                obj[key] = val

            look_ahead_for_garbage = True
            c = stream.read(1)
            while look_ahead_for_garbage:
                if c in ["}", ",", ""]:
                    look_ahead_for_garbage = False
                else:
                    c = stream.read(1)
            stream.seek(-1)

    return obj


def _parse_key_val(stream: _StringStream) -> Tuple[str, Union[str, List, Dict]]:
    key = _parse_key(stream)
    val = _parse_val(stream)
    return key, val


def _parse_key(stream: _StringStream) -> str:
    return stream.advance_past_chars(["="])


def _parse_val(stream: _StringStream) -> Union[str, List, Dict]:
    val: Any
    while True:
        c = stream.read(1)
        if c == "{":
            val = _parse_dict(stream)
            break
        elif c == "[":
            val = _parse_array(stream)
            break
        elif c == '"':
            val = stream.advance_past_string_with_gdb_escapes()
            break
        else:
            val = ""
    return val


def _parse_array(stream: _StringStream) -> list:
    """Parse a GDB MI array (``[val,val,...]``)."""
    arr: list = []
    while True:
        c = stream.read(1)
        if c in _GDB_MI_VALUE_START_CHARS:
            stream.seek(-1)
            val = _parse_val(stream)
            arr.append(val)
        elif c in _WHITESPACE:
            pass
        elif c == ",":
            pass
        elif c == "]":
            break
    return arr


# =========================================================================
# Top-level record matchers (from pygdbmi/gdbmiparser.py)
# =========================================================================

_GDB_MI_COMPONENT_TOKEN = r"(?P<token>\d+)?"
_GDB_MI_COMPONENT_PAYLOAD = r"(?P<payload>,.*)?"
_GDB_MI_RESPONSE_FINISHED_RE = re.compile(r"^\(gdb\)\s*$")

_PARSER_FUNCTION = Callable[[Match, _StringStream], Dict]


def _extract_token(match: Match) -> Optional[int]:
    token = match["token"]
    return int(token) if token is not None else None


def _extract_payload(match: Match, stream: _StringStream) -> Optional[Dict]:
    if match["payload"] is None:
        return None
    stream.advance_past_chars([","])
    return _parse_dict(stream)


def _parse_mi_notify(match: Match, stream: _StringStream) -> Dict:
    return {
        "type": "notify",
        "message": match["message"].strip(),
        "payload": _extract_payload(match, stream),
        "token": _extract_token(match),
    }


def _parse_mi_result(match: Match, stream: _StringStream) -> Dict:
    return {
        "type": "result",
        "message": match["message"],
        "payload": _extract_payload(match, stream),
        "token": _extract_token(match),
    }


def _parse_mi_output(match: Match, stream: _StringStream, output_type: str) -> Dict:
    return {
        "type": output_type,
        "message": None,
        "payload": _unescape(match["payload"]),
    }


def _parse_mi_finished(match: Match, stream: _StringStream) -> Dict:
    return {
        "type": "done",
        "message": None,
        "payload": None,
    }


_GDB_MI_PATTERNS_AND_PARSERS: List[Tuple[Pattern, _PARSER_FUNCTION]] = [
    # Result records: ^done, ^running, ^connected, ^error, ^exit
    (
        re.compile(
            rf"^{_GDB_MI_COMPONENT_TOKEN}\^(?P<message>\S+?){_GDB_MI_COMPONENT_PAYLOAD}$"
        ),
        _parse_mi_result,
    ),
    # Async records: *stopped, =breakpoint-modified, etc.
    (
        re.compile(
            rf"^{_GDB_MI_COMPONENT_TOKEN}[*=](?P<message>\S+?){_GDB_MI_COMPONENT_PAYLOAD}$"
        ),
        _parse_mi_notify,
    ),
    # Console stream: ~"text"
    (
        re.compile(r'~"(?P<payload>.*)"', re.DOTALL),
        functools.partial(_parse_mi_output, output_type="console"),
    ),
    # Log stream: &"text"
    (
        re.compile(r'&"(?P<payload>.*)"', re.DOTALL),
        functools.partial(_parse_mi_output, output_type="log"),
    ),
    # Target stream: @"text"
    (
        re.compile(r'@"(?P<payload>.*)"', re.DOTALL),
        functools.partial(_parse_mi_output, output_type="target"),
    ),
    # Prompt: (gdb)
    (
        _GDB_MI_RESPONSE_FINISHED_RE,
        _parse_mi_finished,
    ),
]


# =========================================================================
# Public API
# =========================================================================

class GDBMIParser:
    """Parse a single GDB/MI output record.

    Usage::

        record = GDBMIParser.parse_response(line)

    The returned dictionary has keys ``type``, ``message``, ``payload``,
    and (for result/notify records) ``token``.

    This class is a faithful extraction of ``pygdbmi.gdbmiparser.parse_response``
    by Chad Smith (MIT License).  See the module docstring for the full
    license text and attribution.
    """

    @staticmethod
    def parse_response(gdb_mi_text: str) -> Dict:
        """Parse a GDB MI text line and return a structured dictionary.

        Returns a dictionary with keys:
            ``type``    — one of ``"result"``, ``"notify"``, ``"console"``,
                          ``"log"``, ``"target"``, ``"done"``, ``"output"``
            ``message`` — the MI class string (e.g. ``"stopped"``, ``"done"``)
                          or ``None`` for stream/output records
            ``payload`` — parsed dict/string payload, or ``None``
            ``token``   — integer command token, or ``None``
        """
        stream = _StringStream(gdb_mi_text)

        for pattern, parser in _GDB_MI_PATTERNS_AND_PARSERS:
            match = pattern.match(gdb_mi_text)
            if match is not None:
                return parser(match, stream)

        # Not a recognised MI record — treat as raw inferior output
        return {
            "type": "output",
            "message": None,
            "payload": gdb_mi_text,
        }

    @staticmethod
    def response_is_finished(gdb_mi_text: str) -> bool:
        """Return ``True`` if *gdb_mi_text* is the ``(gdb)`` prompt."""
        return _GDB_MI_RESPONSE_FINISHED_RE.match(gdb_mi_text) is not None
