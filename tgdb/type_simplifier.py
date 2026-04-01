"""
C++ type simplification for GDB MI output.

GDB returns fully-qualified mangled type names like:
    std::__cxx11::basic_string<char, std::char_traits<char>, std::allocator<char>>

This module collapses them into human-readable forms:
    std::string

Rules are applied iteratively until no more changes occur.  Each rule is
a (pattern, replacement) pair.  Rules are intentionally kept in a flat,
data-driven table so they are easy to audit, extend, and maintain as
C++ ABIs evolve.
"""
from __future__ import annotations

import re

# ---------------------------------------------------------------------------
# Simplification rules — ordered list of (compiled_regex, replacement).
#
# Applied repeatedly in order until the type string stabilizes.
# Keep each rule focused on one transformation.  Comments explain what
# each rule targets.
#
# To add support for a new ABI or library, append rules here.
# ---------------------------------------------------------------------------

_RULES: list[tuple[re.Pattern, str]] = []


def _add(pattern: str, repl: str) -> None:
    _RULES.append((re.compile(pattern), repl))


# -- Step 1: std::__cxx11:: → std:: (libstdc++ ABI namespace) --------------
_add(r"\bstd::__cxx11::", "std::")

# -- Step 2: std::__1:: → std:: (libc++ ABI namespace) ---------------------
_add(r"\bstd::__1::", "std::")

# -- Step 3: basic_string<char, ...> → string -----------------------------
# Matches basic_string with char and default traits/allocator, with or
# without the std:: prefix on traits/allocator.
_add(
    r"\bbasic_string<char,\s*std::char_traits<char>,\s*std::allocator<char>\s*>",
    "string",
)
# basic_string<char> (already partially simplified)
_add(r"\bbasic_string<char>", "string")

# -- Step 4: basic_string<wchar_t, ...> → wstring -------------------------
_add(
    r"\bbasic_string<wchar_t,\s*std::char_traits<wchar_t>,\s*std::allocator<wchar_t>\s*>",
    "wstring",
)
_add(r"\bbasic_string<wchar_t>", "wstring")

# -- Step 5: basic_string<char16_t, ...> → u16string ----------------------
_add(
    r"\bbasic_string<char16_t,\s*std::char_traits<char16_t>,\s*std::allocator<char16_t>\s*>",
    "u16string",
)
_add(r"\bbasic_string<char16_t>", "u16string")

# -- Step 6: basic_string<char32_t, ...> → u32string ----------------------
_add(
    r"\bbasic_string<char32_t,\s*std::char_traits<char32_t>,\s*std::allocator<char32_t>\s*>",
    "u32string",
)
_add(r"\bbasic_string<char32_t>", "u32string")

# -- Step 7: basic_string<char8_t, ...> → u8string (C++20) ----------------
_add(
    r"\bbasic_string<char8_t,\s*std::char_traits<char8_t>,\s*std::allocator<char8_t>\s*>",
    "u8string",
)
_add(r"\bbasic_string<char8_t>", "u8string")

# -- Step 8: basic_string_view<char, ...> → string_view -------------------
_add(
    r"\bbasic_string_view<char,\s*std::char_traits<char>\s*>",
    "string_view",
)
_add(r"\bbasic_string_view<char>", "string_view")


# -- Step 9: Strip default allocator from containers ----------------------
# vector<T, std::allocator<T>> → vector<T>
# list<T, std::allocator<T>> → list<T>
# deque<T, std::allocator<T>> → deque<T>
# forward_list<T, std::allocator<T>> → forward_list<T>
# etc.
#
# This uses a function-based replacer to handle nested templates in T.
def _strip_default_allocator(m: re.Match) -> str:
    container = m.group(1)
    inner = m.group(2)
    return f"{container}<{inner}>"


_RULES.append((
    re.compile(
        r"\b(vector|list|deque|forward_list|set|multiset|unordered_set|"
        r"unordered_multiset)"
        r"<(.+?),\s*std::allocator<\2>\s*>"
    ),
    _strip_default_allocator,
))

# -- Step 10: Strip default comparator + allocator from ordered maps/sets --
# map<K, V, std::less<K>, std::allocator<std::pair<const K, V>>> → map<K, V>
# Also handles std::less<> (transparent comparator)
_RULES.append((
    re.compile(
        r"\b(map|multimap)"
        r"<(.+?),\s*(.+?),\s*std::less<(?:\2)?>,\s*std::allocator<std::pair<const \2,\s*\3>\s*>\s*>"
    ),
    lambda m: f"{m.group(1)}<{m.group(2)}, {m.group(3)}>",
))

# map/multimap with just less<> or less<K> stripped (allocator already gone)
_RULES.append((
    re.compile(
        r"\b(map|multimap)"
        r"<(.+?),\s*(.+?),\s*std::less<(?:\2)?>\s*>"
    ),
    lambda m: f"{m.group(1)}<{m.group(2)}, {m.group(3)}>",
))

# set/multiset with std::less
_RULES.append((
    re.compile(
        r"\b(set|multiset)"
        r"<(.+?),\s*std::less<(?:\2)?>,\s*std::allocator<\2>\s*>"
    ),
    lambda m: f"{m.group(1)}<{m.group(2)}>",
))

_RULES.append((
    re.compile(
        r"\b(set|multiset)"
        r"<(.+?),\s*std::less<(?:\2)?>\s*>"
    ),
    lambda m: f"{m.group(1)}<{m.group(2)}>",
))

# -- Step 11: Strip hash/equal/allocator from unordered containers ---------
# unordered_map<K, V, std::hash<K>, std::equal_to<K>, std::allocator<...>>
_RULES.append((
    re.compile(
        r"\b(unordered_map|unordered_multimap)"
        r"<(.+?),\s*(.+?),\s*std::hash<\2>,\s*std::equal_to<\2>,\s*"
        r"std::allocator<std::pair<const \2,\s*\3>\s*>\s*>"
    ),
    lambda m: f"{m.group(1)}<{m.group(2)}, {m.group(3)}>",
))

_RULES.append((
    re.compile(
        r"\b(unordered_set|unordered_multiset)"
        r"<(.+?),\s*std::hash<\2>,\s*std::equal_to<\2>,\s*"
        r"std::allocator<\2>\s*>"
    ),
    lambda m: f"{m.group(1)}<{m.group(2)}>",
))

# -- Step 12: unique_ptr<T, std::default_delete<T>> → unique_ptr<T> -------
_RULES.append((
    re.compile(
        r"\bunique_ptr<(.+?),\s*std::default_delete<\1>\s*>"
    ),
    lambda m: f"unique_ptr<{m.group(1)}>",
))

# -- Step 13: shared_ptr internal detail types ----------------------------
# Skip __shared_ptr_access, __shared_ptr etc.
_add(r"\b__shared_ptr<", "shared_ptr<")
_add(r"\b__shared_ptr_access<[^>]*>::", "")

# -- Step 14: Collapse extra whitespace inside angle brackets --------------
_add(r"\s+>", ">")
_add(r">\s+>", ">>")
_add(r"<\s+", "<")

# -- Step 15: Collapse multiple spaces ------------------------------------
_add(r"  +", " ")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

_MAX_ITERATIONS = 10  # safety limit to prevent infinite loops


def simplify_type(type_str: str) -> str:
    """Simplify a C++ type string from GDB MI output.

    Applies all rules iteratively until the string stabilizes or the
    iteration limit is reached.

    >>> simplify_type("std::__cxx11::basic_string<char, std::char_traits<char>, std::allocator<char> >")
    'std::string'
    >>> simplify_type("std::vector<int, std::allocator<int> >")
    'std::vector<int>'
    >>> simplify_type("int")
    'int'
    """
    prev = None
    for _ in range(_MAX_ITERATIONS):
        if type_str == prev:
            break
        prev = type_str
        for pattern, repl in _RULES:
            if callable(repl) and not isinstance(repl, str):
                type_str = pattern.sub(repl, type_str)
            else:
                type_str = pattern.sub(repl, type_str)
    return type_str.strip()
