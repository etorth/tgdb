# python-style-format

Use this repository-local skill after Python code changes.

## Goal

Keep Python code readable, explicit, and easy to maintain in this repository.

## Rules

1. Follow PEP 8, but do **not** enforce a hard line-length limit.
2. Break lines only when the broken form is easier to read.
3. For function and method signatures, avoid half-inline multiline forms. Either keep the full signature on one line, or if you wrap it, put each parameter on its own line.
4. For type annotations, do not split a single generic argument across multiple lines. Keep forms like `list[str]`, `tuple[int]`, and `Optional[Widget]` on one line unless the whole annotation is being wrapped in a clearly better multiline form.
5. Prefer **f-strings** for string formatting.
6. Avoid dense comprehensions and other “too pythonic” constructs when an explicit loop is clearer.
7. Leave **two blank lines** between member functions in classes in this repository.
8. Keep Python files around **500 lines** when practical by splitting large modules by responsibility.
9. When a module exposes a reusable public type, document its interface clearly at the module/class level: construction, injected dependencies, state-mutation methods, public API surface, and the behavior callers may treat as a black-box contract.

## Refactoring checklist

1. Check whether the touched Python file is getting too large.
2. Split helpers, state reconciliation, and UI/tree logic into separate modules when that improves readability.
3. Rewrite hard-to-scan comprehensions as explicit loops.
4. Rewrite old-style logging/string formatting with f-strings when the code is being touched.
5. Add or refresh module/class docstrings when the code defines a reusable public type.
6. Re-run the repository Python syntax check after the refactor.
