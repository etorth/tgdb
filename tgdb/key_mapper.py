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
                result = self._resolve_buffer(root, flushed)
                buf.append(key_token)
                return result

        buf.append(key_token)

        # Walk trie with buffered sequence
        node = root
        for break_pos, token in enumerate(buf):
            if token not in node.children:
                # No map can extend the buffer at this point.  Resolve
                # the prefix greedily so any complete-prefix mapping
                # (e.g. "aa" given "aa"→b and "aaa"→c) still fires
                # before the unmappable tail emits.
                #
                # The breaking token (and anything after it in the
                # buffer) might itself begin a new mapping — re-buffer
                # those tokens that DO extend a mapping at the trie
                # root so the next keystroke can complete the new
                # sequence.  Without this re-buffer step, typing
                # ``a`` (extends ``aa``→b) then ``b`` (starts ``bc``→y)
                # then ``c`` would emit three literal keys instead of
                # ``a`` followed by the ``bc``→y expansion: the ``b``
                # would be flushed as raw before the ``c`` arrived.
                head = buf[:break_pos]
                tail = buf[break_pos:]
                buf.clear()
                result = self._resolve_buffer(root, head) if head else []
                for t in tail:
                    if t in root.children:
                        buf.append(t)
                    else:
                        result.append(t)
                return result
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

        Resolves the buffer greedily so a complete mapping held back only
        because it could be a prefix of a longer sequence still fires
        rather than being returned as raw keys.  See ``_resolve_buffer``.
        """
        buf = self._buf.get(mode, [])
        if not buf:
            return []
        flushed = list(buf)
        buf.clear()
        root = self._roots.get(mode, TrieNode())
        return self._resolve_buffer(root, flushed)


    def _resolve_buffer(self, root: TrieNode, tokens: list[str]) -> list[str]:
        """Greedy longest-prefix resolution for a buffered token sequence.

        Walks the trie from each position, tracking the longest prefix
        whose terminal node has a value, then emits that mapping's
        expansion and continues from the position after it.  When no
        prefix at the current position matches, the single token passes
        through unchanged.  Mirrors vim's timeout behavior: given
        ``aa → b`` and ``aaa → c``, a buffer of ``["a","a"]`` flushes to
        ``["b"]``, and ``["x","a","a"]`` flushes to ``["x","b"]``.
        """
        result: list[str] = []
        i = 0
        n = len(tokens)
        while i < n:
            node = root
            j = i
            last_match_end = i
            last_match_value: list[str] | None = None
            while j < n and tokens[j] in node.children:
                node = node.children[tokens[j]]
                j += 1
                if node.value is not None:
                    last_match_end = j
                    last_match_value = node.value

            if last_match_value is not None:
                result.extend(last_match_value)
                i = last_match_end
            else:
                result.append(tokens[i])
                i += 1

        return result
