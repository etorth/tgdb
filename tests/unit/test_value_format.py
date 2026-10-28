"""Value-string helpers: UTF-8 octal escapes, MI quoting, address normalisation."""

import pytest

from tgdb.gdb_controller.types import normalize_addr, quote_mi_string
from tgdb.gdb_controller.value_format import decode_utf8_octal_escapes

# One table shared with tests/unit/test_pysetup.py, which runs the same cases
# through the GDB-side copy of this decoder.
UTF8_OCTAL_CASES = [
    # GDB prints non-ASCII bytes of a char array as octal escapes.
    ('"caf\\303\\251"', '"café"'),
    ('"\\360\\237\\230\\200 ok"', '"😀 ok"'),
    # An escaped backslash followed by digits is not an octal escape.
    ('"C:\\\\2024\\\\x"', '"C:\\\\2024\\\\x"'),
    # Plain ASCII escapes and invalid UTF-8 stay as they are.
    ('"\\001\\002"', '"\\001\\002"'),
    ('"\\377\\376"', '"\\377\\376"'),
    ("no escapes at all", "no escapes at all"),
]


@pytest.mark.parametrize(("raw", "expected"), UTF8_OCTAL_CASES)
def test_decode_utf8_octal_escapes(raw, expected):
    assert decode_utf8_octal_escapes(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("main", '"main"'),
        ('a "b"', '"a \\"b\\""'),
        ("C:\\x", '"C:\\\\x"'),
    ],
)
def test_quote_mi_string(raw, expected):
    assert quote_mi_string(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("(int *) 0x7ffe1234", "0x7ffe1234"),
        ("0x7ffe1234", "0x7ffe1234"),
    ],
)
def test_normalize_addr(raw, expected):
    assert normalize_addr(raw) == expected
