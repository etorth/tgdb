---
applyTo: "**/*.py"
---

# python-style-format

Apply these rules after every Python code change in this repository.
The codebase targets **Python 3.14**.

- Do not enforce any hard file-length limit. Split a Python module **only** when it has grown large *and* its contents naturally decompose into independent responsibilities that read better as separate files. A long but cohesive module (one type, one concern) should be left as a single file.
- Follow **PEP 8** for naming, spacing, and structure, but **do not** treat line length as a hard rule. Break a line only when the broken form is genuinely easier to read.
- For function and method signatures, avoid half-inline multiline forms. Either keep the full signature on one line, or if you wrap it, put each parameter on its own line.
- For type annotations, do not split a single generic argument across multiple lines. Keep forms like `list[str]`, `tuple[int]`, and `Widget | None` on one line unless the entire annotation is being wrapped in a genuinely clearer multiline layout.
- Prefer **f-strings** over `%` formatting or logger argument tuples when writing or updating string formatting.
- Avoid dense or overly clever Python constructs when the logic is non-trivial. In particular, prefer explicit `for` loops over multi-condition comprehensions when the loop is easier to read.
- Leave **two blank lines between member functions** in classes in this codebase.
- Prefer direct, readable control flow over compact "pythonic" one-liners when there is any meaningful branching or filtering.
- When splitting a large Python file, preserve behavior first, then improve names and local structure without changing unrelated logic.
- When a Python module exposes a reusable public type, document its clean interface. Add module/class docstrings that explain how to construct it, which dependencies or callbacks must be injected, which public methods mutate its state, which methods are intended as the public API, and what black-box behavior callers can rely on.
- **Use Python 3.14 syntax only.** Do not add `from __future__ import annotations`; quote forward references that depend on `TYPE_CHECKING`-only imports as string literals instead. Do not import `Optional`, `Union`, `Dict`, `List`, `Tuple`, `Set`, `FrozenSet`, or `Type` from `typing` — use `T | None`, `A | B`, and the builtin generics `dict[...]`, `list[...]`, `tuple[...]`, `set[...]`, `frozenset[...]`, `type[...]`. Import `Callable`, `Iterable`, `Iterator`, `Awaitable`, `Coroutine`, `Sequence`, `Mapping`, `MutableMapping`, etc. from `collections.abc`, not from `typing`. `typing` is reserved for things that genuinely live there in 3.14 (`TYPE_CHECKING`, `cast`, `Protocol`, `TypeAlias`, `TypeVar`, `ParamSpec`, `Self`, `Literal`, `Annotated`, `Any`, `NoReturn`, `Never`, `overload`).
