"""Key mapper — trie-based prefix matching with timeout, mirrors cgdb's KUI.

Keys are represented as Textual key-name tokens (e.g. ``"escape"``,
``"enter"``, ``"ctrl+w"``, ``"s"``).  A single printable character is its
own token (``"s"``, ``":"``, ``"("``).  This makes the trie compatible with
the key names Textual delivers via ``event.key``.
"""

import time


class TrieNode:
    def __init__(self) -> None:
        self.children: dict[str, "TrieNode"] = {}
        self.value: list[str] | None = None  # Leaf: RHS token list


class KeyMapper:
    """
    Maintains separate maps for TGDB mode and GDB/insert mode.

    Usage::
        km = KeyMapper()
        km.map("tgdb", ["s"], ["escape", ":", "s", "t", "e", "p", "enter"])
        tokens = km.feed("tgdb", key_token)   # returns [] while buffering,
                                               # or list of tokens to dispatch
    """

    def __init__(self, timeout_ms: int = 1000, ttimeout_ms: int = 100) -> None:
        self._roots: dict[str, TrieNode] = {"tgdb": TrieNode(), "gdb": TrieNode()}
        self._buf: dict[str, list[str]] = {"tgdb": [], "gdb": []}
        self._last_key_time: dict[str, float] = {}  # per-mode, so cross-mode keypresses don't reset another mode's timeout
        self.timeout_ms = timeout_ms
        self.ttimeout_ms = ttimeout_ms
        self.timeout_enabled = True
        self.ttimeout_enabled = True

    # ------------------------------------------------------------------
    # Map management
    # ------------------------------------------------------------------

    def map(self, mode: str, lhs: list[str], rhs: list[str]) -> None:
        """Register a mapping from LHS token sequence to RHS token sequence."""
        root = self._roots.setdefault(mode, TrieNode())
        node = root
        for token in lhs:
            node = node.children.setdefault(token, TrieNode())
        node.value = list(rhs)


    def unmap(self, mode: str, lhs: list[str]) -> bool:
        """Remove a mapping.  Returns True if the mapping existed."""
        root = self._roots.get(mode)
        if not root:
            return False
        node = root
        path: list[tuple[TrieNode, str]] = []
        for token in lhs:
            if token not in node.children:
                return False
            path.append((node, token))
            node = node.children[token]
        if node.value is None:
            return False
        node.value = None
        # Prune empty leaves
        for parent, token in reversed(path):
            child = parent.children[token]
            if not child.children and child.value is None:
                del parent.children[token]
        return True

    # ------------------------------------------------------------------
    # Key feeding
    # ------------------------------------------------------------------

    def feed(self, mode: str, key_token: str) -> list[str]:
        """Feed one key-name token; return list of tokens to dispatch.

        Returns ``[]`` if the token is buffered (awaiting a possible longer
        match).  Returns a non-empty list of tokens otherwise: either the
        expansion when a map fires, or the buffered tokens flushed as
        pass-through when no map matches.
        """
        now = time.monotonic()
        last = self._last_key_time.get(mode, 0.0)
        elapsed_ms = (now - last) * 1000 if last else 9999
        self._last_key_time[mode] = now

        buf = self._buf.setdefault(mode, [])
        root = self._roots.get(mode, TrieNode())

        # If timeout elapsed, flush stale buffer and start fresh.
        # Escape-initiated sequences use ttimeout/ttimeout_ms (terminal key codes);
        # all other sequences use timeout/timeout_ms (key mappings).
        if buf:
            is_escape_seq = buf[0] == "escape"
            if is_escape_seq and self.ttimeout_enabled:
                timed_out = elapsed_ms > self.ttimeout_ms
            elif self.timeout_enabled:
                timed_out = elapsed_ms > self.timeout_ms
            else:
                timed_out = False

            if timed_out:
                flushed = list(buf)
                buf.clear()
                result: list[str] = []
                for t in flushed:
                    result.extend(self._resolve(root, [t]))
                buf.append(key_token)
                return result

        buf.append(key_token)

        # Walk trie with buffered sequence
        node = root
        for token in buf:
            if token not in node.children:
                # No map possible — flush everything as pass-through
                flushed = list(buf)
                buf.clear()
                return flushed
            node = node.children[token]

        if node.value is not None:
            # Exact match — but might be a prefix of a longer map
            if node.children:
                return []  # wait for more input / timeout
            # Definite leaf: fire the map
            expansion = list(node.value)
            buf.clear()
            return expansion

        # Internal node — still building prefix
        return []


    def flush(self, mode: str) -> list[str]:
        """Force-flush any buffered tokens (called on timeout).

        Resolves each token through the trie so that a complete mapping that
        was held back only because it could be a prefix of a longer sequence
        is still fired rather than returned as a raw key.
        """
        buf = self._buf.get(mode, [])
        if not buf:
            return []
        flushed = list(buf)
        buf.clear()
        root = self._roots.get(mode, TrieNode())
        result: list[str] = []
        for t in flushed:
            result.extend(self._resolve(root, [t]))
        return result


    def _resolve(self, root: TrieNode, tokens: list[str]) -> list[str]:
        node = root
        for t in tokens:
            if t not in node.children:
                return tokens
            node = node.children[t]
        if node.value is not None:
            return list(node.value)
        return tokens
