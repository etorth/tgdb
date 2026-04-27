# python-style-format

Use this repository-local skill after Python code changes.

## Goal

Keep Python code readable, explicit, and easy to maintain in this
repository. The codebase targets **Python 3.14**; older compatibility
shims (`from __future__ import annotations`) and legacy typing forms
(`Optional`, `Union`, `Dict`, `List`, `Tuple`, `Set`, `FrozenSet`,
`Type`, `typing.Callable`, `typing.Iterable`, ...) are not used.

## Rules

1. Follow PEP 8, but do **not** enforce a hard line-length limit.
2. Break lines only when the broken form is easier to read.
3. For function and method signatures, avoid half-inline multiline forms. Either keep the full signature on one line, or if you wrap it, put each parameter on its own line.
4. For type annotations, do not split a single generic argument across multiple lines. Keep forms like `list[str]`, `tuple[int]`, and `Widget | None` on one line unless the whole annotation is being wrapped in a clearly better multiline form.
5. Prefer **f-strings** for string formatting.
6. Avoid dense comprehensions and other "too pythonic" constructs when an explicit loop is clearer.
7. Leave **two blank lines** between member functions in classes in this repository.
8. Do not enforce any hard file-length limit. Split a Python module **only** when it has grown large *and* its contents naturally decompose into independent responsibilities that read better as separate files. A long but cohesive module (one type, one concern) should be left alone.
9. When a module exposes a reusable public type, document its interface clearly at the module/class level: construction, injected dependencies, state-mutation methods, public API surface, and the behavior callers may treat as a black-box contract.
10. **Target Python 3.14 syntax only.**
    - Never add `from __future__ import annotations`. Annotations are evaluated at definition time on 3.14; forward references must be quoted as string literals instead (e.g. `def m(self: "TGDBApp") -> None: ...`).
    - Never import `Optional`, `Union`, `Dict`, `List`, `Tuple`, `Set`, `FrozenSet`, or `Type` from `typing`. Use `T | None`, `A | B`, and the builtin generics `dict[...]`, `list[...]`, `tuple[...]`, `set[...]`, `frozenset[...]`, `type[...]`.
    - Never import `Callable`, `Iterable`, `Iterator`, `Awaitable`, `Coroutine`, `Sequence`, `Mapping`, `MutableMapping`, `AsyncIterator`, `AsyncIterable`, etc. from `typing`. Import them from `collections.abc` instead.
    - `typing` may still be used for things that genuinely live there in 3.14: `TYPE_CHECKING`, `cast`, `Protocol`, `TypeAlias`, `TypeVar`, `ParamSpec`, `Self`, `Literal`, `Annotated`, `Any`, `NoReturn`, `Never`, `overload`, and similar.

## Refactoring checklist

1. Consider whether a touched Python file is *both* large *and* made up of separable responsibilities; only then should it be split.
2. When splitting is warranted, peel helpers, state reconciliation, and UI/tree logic into separate modules.
3. Rewrite hard-to-scan comprehensions as explicit loops.
4. Rewrite old-style logging/string formatting with f-strings when the code is being touched.
5. Add or refresh module/class docstrings when the code defines a reusable public type.
6. Verify no `from __future__ import annotations` was reintroduced and no banned `typing` names (see rule 10) are imported from `typing`.
7. Re-run the repository Python syntax check after the refactor.
