"""Key mapper trie: prefix buffering, flushing, timeouts, unmap."""

from tgdb.key_mapper import KeyMapper


def make(*maps: tuple[str, str]) -> KeyMapper:
    km = KeyMapper()
    km.timeout_enabled = False
    for lhs, rhs in maps:
        km.map("tgdb", list(lhs), list(rhs))
    return km


def feed_all(km: KeyMapper, keys: str) -> list[str]:
    out: list[str] = []
    for key in keys:
        out.extend(km.feed("tgdb", key))
    return out


def test_unmapped_key_passes_through():
    km = make(("ab", "X"))
    assert km.feed("tgdb", "z") == ["z"]


def test_two_key_map_fires_after_second_key():
    km = make(("ab", "X"))
    assert km.feed("tgdb", "a") == []
    assert km.has_pending("tgdb")
    assert km.feed("tgdb", "b") == ["X"]
    assert not km.has_pending("tgdb")


def test_broken_prefix_passes_keys_through():
    km = make(("ab", "X"))
    assert feed_all(km, "ac") == ["a", "c"]


def test_broken_prefix_fires_single_key_leaf_immediately():
    """Regression: the breaking key's own map was buffered until the next key."""
    km = make(("ab", "Q"), ("x", "Y"))
    assert feed_all(km, "ax") == ["a", "Y"]
    assert not km.has_pending("tgdb")


def test_complete_map_that_is_also_a_prefix_waits_then_flushes():
    km = make(("aa", "B"), ("aaa", "C"))
    assert feed_all(km, "aa") == []
    assert km.flush("tgdb") == ["B"]
    assert feed_all(km, "aaa") == ["C"]


def test_broken_prefix_rebuffers_start_of_new_map():
    km = make(("aa", "B"), ("bc", "Y"))
    assert feed_all(km, "abc") == ["a", "Y"]


def test_timeout_flushes_stale_buffer_and_processes_new_key(monkeypatch):
    km = make(("ab", "X"))
    km.timeout_enabled = True
    km.timeout_ms = 10
    clock = iter([100.0, 100.5])
    monkeypatch.setattr("tgdb.key_mapper.time.monotonic", lambda: next(clock))
    assert km.feed("tgdb", "a") == []
    assert km.feed("tgdb", "z") == ["a", "z"]


def test_unmap_removes_mapping():
    km = make(("ab", "X"))
    assert km.unmap("tgdb", ["a", "b"])
    assert feed_all(km, "ab") == ["a", "b"]
    assert not km.unmap("tgdb", ["a", "b"])


def test_modes_are_independent():
    km = make(("ab", "X"))
    assert km.feed("gdb", "a") == ["a"]
