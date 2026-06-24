"""Helpers for normalising GDB value display strings."""

import re

_UTF8_OCTAL_RUN_RE = re.compile(r"(?<!\\)(?:\\[0-7]{3})+")


def decode_utf8_octal_escapes(value: str) -> str:
    """Decode GDB octal-escaped UTF-8 byte runs in a value string.

    GDB may render non-BMP UTF-8 characters as octal byte escapes even when its
    target charset is UTF-8, for example ``"\\360\\237\\232\\200"`` for 🚀.
    Convert only runs that are valid UTF-8 and contain at least one non-ASCII
    byte; leave ordinary C escapes such as ``\\012`` and invalid byte data as
    GDB printed them.
    """
    if "\\" not in value:
        return value

    def replace(match: re.Match) -> str:
        text = match.group(0)
        raw = bytes(
            int(text[index + 1:index + 4], 8)
            for index in range(0, len(text), 4)
        )
        if not any(byte >= 0x80 for byte in raw):
            return text

        try:
            decoded = raw.decode("utf-8")
        except UnicodeDecodeError:
            return text

        if any(ord(char) < 32 for char in decoded):
            return text

        return decoded

    return _UTF8_OCTAL_RUN_RE.sub(replace, value)
