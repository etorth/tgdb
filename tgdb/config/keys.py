"""Key-sequence decoding helpers for the configuration package."""

class ConfigKeyMixin:
    """Mixin providing cgdb-style key sequence decoding."""

    _KEY_TOKENS: dict[str, str] = {
        "space": "space",
        "enter": "enter",
        "return": "enter",
        "cr": "enter",
        "nl": "ctrl+j",
        "tab": "tab",
        "esc": "escape",
        "escape": "escape",
        "bs": "backspace",
        "backspace": "backspace",
        "del": "delete",
        "delete": "delete",
        "insert": "insert",
        "nul": "ctrl+@",
        "lt": "<",
        "bslash": "\\",
        "bar": "|",
        "up": "up",
        "down": "down",
        "left": "left",
        "right": "right",
        "pageup": "pageup",
        "pagedown": "pagedown",
        "home": "home",
        "end": "end",
        "f1": "f1",
        "f2": "f2",
        "f3": "f3",
        "f4": "f4",
        "f5": "f5",
        "f6": "f6",
        "f7": "f7",
        "f8": "f8",
        "f9": "f9",
        "f10": "f10",
        "f11": "f11",
        "f12": "f12",
    }

    def _decode_keyseq_tokens(self, s: str) -> list[str]:
        tokens: list[str] = []
        index = 0
        while index < len(s):
            if s[index] == "<":
                end = s.find(">", index)
                if end != -1:
                    name = s[index + 1 : end].lower()
                    tokens.append(self._key_token(name))
                    index = end + 1
                    continue
            ch = s[index]
            if ch == " ":
                tokens.append("space")
            else:
                tokens.append(ch)
            index += 1
        return tokens


    # US-keyboard shifted equivalents.  ``<S-1>`` should resolve to ``!``
    # the same way pressing Shift-1 produces ``!`` on a standard layout —
    # the upper-case-letter case (``<S-a>`` → ``A``) is handled inline
    # because Python's ``.upper()`` already does the right thing for
    # alphabetic characters.  Without this table, ``<S-1>`` returned a
    # bare ``"1"`` and the user's mapping silently bound to the
    # unshifted key.
    _SHIFTED_CHARS: dict[str, str] = {
        "1": "!", "2": "@", "3": "#", "4": "$", "5": "%",
        "6": "^", "7": "&", "8": "*", "9": "(", "0": ")",
        "-": "_", "=": "+", "[": "{", "]": "}",
        ";": ":", "'": '"', ",": "<", ".": ">", "/": "?",
        "\\": "|", "`": "~",
    }


    def _key_token(self, name: str) -> str:
        if name in self._KEY_TOKENS:
            return self._KEY_TOKENS[name]
        if name.startswith("c-") and len(name) == 3:
            return f"ctrl+{name[2].lower()}"
        if name.startswith("s-") and len(name) == 3:
            base = name[2]
            if base in self._SHIFTED_CHARS:
                return self._SHIFTED_CHARS[base]
            return base.upper()
        if (name.startswith("m-") or name.startswith("a-")) and len(name) == 3:
            return f"escape+{name[2]}"
        return f"<{name}>"
