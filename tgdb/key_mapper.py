"""Key mapper — trie-based prefix matching with timeout, mirrors cgdb's KUI."""
from __future__ import annotations
import time
from typing import Optional


class TrieNode:
    def __init__(self) -> None:
        self.children: dict[str, "TrieNode"] = {}
        self.value: Optional[str] = None  # Leaf: expansion string


class KeyMapper:
    """
    Maintains separate maps for CGDB mode and GDB/insert mode.

    Usage::
        km = KeyMapper()
        km.map("cgdb", "<F5>", "run\\n")
        result = km.feed("cgdb", key)   # returns None while prefix may match,
                                         # or (lhs_matched, rhs) | (key, None)
    """

    def __init__(self, timeout_ms: int = 1000, ttimeout_ms: int = 100) -> None:
        self._roots: dict[str, TrieNode] = {"cgdb": TrieNode(), "gdb": TrieNode()}
        self._buf: dict[str, list[str]] = {"cgdb": [], "gdb": []}
        self._last_key_time: float = 0.0
        self.timeout_ms = timeout_ms
        self.ttimeout_ms = ttimeout_ms
        self.timeout_enabled = True
        self.ttimeout_enabled = True

    # ------------------------------------------------------------------
    # Map management
    # ------------------------------------------------------------------

    def map(self, mode: str, lhs: str, rhs: str) -> None:
        root = self._roots.setdefault(mode, TrieNode())
        node = root
        for ch in lhs:
            node = node.children.setdefault(ch, TrieNode())
        node.value = rhs

    def unmap(self, mode: str, lhs: str) -> bool:
        root = self._roots.get(mode)
        if not root:
            return False
        node = root
        path: list[tuple[TrieNode, str]] = []
        for ch in lhs:
            if ch not in node.children:
                return False
            path.append((node, ch))
            node = node.children[ch]
        if node.value is None:
            return False
        node.value = None
        # Prune empty leaves
        for parent, ch in reversed(path):
            child = parent.children[ch]
            if not child.children and child.value is None:
                del parent.children[ch]
        return True

    # ------------------------------------------------------------------
    # Key feeding
    # ------------------------------------------------------------------

    def feed(self, mode: str, key: str) -> list[str]:
        """
        Feed one keypress; return list of keys to dispatch.

        Returns [] if the key is buffered (awaiting possible longer match).
        """
        now = time.monotonic()
        elapsed_ms = (now - self._last_key_time) * 1000 if self._last_key_time else 9999
        self._last_key_time = now

        buf = self._buf.setdefault(mode, [])
        root = self._roots.get(mode, TrieNode())

        # Check if timeout elapsed on previous buffer
        if buf and self.timeout_enabled and elapsed_ms > self.timeout_ms:
            flushed = list(buf)
            buf.clear()
            result = []
            for k in flushed:
                result.extend(self._resolve(root, [k]))
            buf.append(key)
            return result

        buf.append(key)

        # Walk trie with buffered sequence
        node = root
        for ch in buf:
            if ch not in node.children:
                # No match possible
                flushed = list(buf)
                buf.clear()
                return flushed  # pass through unmapped
            node = node.children[ch]

        if node.value is not None:
            # Exact match — but might be a prefix of a longer match
            if node.children:
                # Ambiguous: wait for more input (rely on timeout)
                return []
            # Definite leaf
            buf.clear()
            return list(node.value)  # expand: each char becomes a keypress
        # node is an internal node — still building prefix
        return []

    def flush(self, mode: str) -> list[str]:
        """Force-flush any buffered keys (called on timeout)."""
        buf = self._buf.get(mode, [])
        flushed = list(buf)
        buf.clear()
        return flushed

    def _resolve(self, root: TrieNode, keys: list[str]) -> list[str]:
        node = root
        for k in keys:
            if k not in node.children:
                return keys
            node = node.children[k]
        if node.value is not None:
            return list(node.value)
        return keys
