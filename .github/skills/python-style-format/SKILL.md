---
name: python-style-format
description: >-
    Skill for enforcing Python code style and formatting rules in the tgdb
    repository. Apply after any Python code change to keep code readable,
    explicit, and split by responsibility.
user-invocable: true
---

# python-style-format

Apply this skill after Python code changes in this repository.

## Intent

Keep Python code explicit, readable, and split by responsibility. The
codebase targets **Python 3.14**; older compatibility shims and legacy
typing forms are not allowed.

## Rules

1. Follow PEP 8, but do not enforce a hard line-length limit.
2. Break lines only when the broken form is easier to understand.
3. For function and method signatures, avoid half-inline multiline forms. Either keep the full signature on one line, or if you wrap it, put each parameter on its own line.
4. For type annotations, do not split a single generic argument across multiple lines. Keep forms like `list[str]`, `tuple[int]`, and `Widget | None` on one line unless the whole annotation is being wrapped in a clearly better multiline form.
5. Prefer f-strings over older string-formatting styles.
6. Prefer explicit loops over dense comprehensions when the loop is clearer.
7. Leave two blank lines between member functions in classes in this repository.
8. Do not enforce any hard file-length limit. Split a Python module **only** when it has grown large *and* its contents naturally decompose into independent responsibilities that read better as separate files. If an oversized module is already cohesive (one type, one concern), leave it alone.
9. When a module exposes a reusable public type, document the clean interface at the module/class level: construction, injected dependencies, state-mutation methods, public API surface, and the black-box behavior callers can rely on.
10. **Target Python 3.14 syntax only.** Do not add `from __future__ import annotations` (annotations are evaluated at definition time and forward references must be quoted as strings instead). Do not import `Optional`, `Union`, `Dict`, `List`, `Tuple`, `Set`, `FrozenSet`, or `Type` from `typing` — use `T | None`, `A | B`, and the builtin generics `dict[...]`, `list[...]`, `tuple[...]`, `set[...]`, `frozenset[...]`, and `type[...]` instead. Import `Callable`, `Iterable`, `Iterator`, `Awaitable`, `Coroutine`, `Sequence`, `Mapping`, `MutableMapping`, etc. from `collections.abc`, not from `typing`. `typing` is reserved for things that genuinely live there in 3.14, e.g. `TYPE_CHECKING`, `cast`, `Protocol`, `TypeAlias`, `TypeVar`, `ParamSpec`, `Self`, `Literal`, `Annotated`, `Any`, `NoReturn`, `Never`, `overload`.

## Checklist

1. Consider splitting a touched Python file only when it is both large *and* the contents fall into clearly separable responsibilities. Don't split a cohesive module just because it is long.
2. When splitting is justified, peel helpers, tree/UI logic, and state reconciliation out into separate modules.
3. Rewrite overly clever Python into direct control flow.
4. Add or refresh module/class docstrings when the code defines a reusable public type.
5. Verify no `from __future__ import annotations` is present and no banned `typing` names (`Optional`, `Union`, `Dict`, `List`, `Tuple`, `Set`, `FrozenSet`, `Type`, `Callable`, `Iterable`, `Iterator`, `Awaitable`, `Coroutine`, `Sequence`, `Mapping`, `MutableMapping`) are imported from `typing`.
6. Re-run the repository Python syntax check after the refactor.
