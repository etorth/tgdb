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

Keep Python code explicit, readable, and split by responsibility.

## Rules

1. Follow PEP 8, but do not enforce a hard line-length limit.
2. Break lines only when the broken form is easier to understand.
3. For function and method signatures, avoid half-inline multiline forms. Either keep the full signature on one line, or if you wrap it, put each parameter on its own line.
4. For type annotations, do not split a single generic argument across multiple lines. Keep forms like `list[str]`, `tuple[int]`, and `Optional[Widget]` on one line unless the whole annotation is being wrapped in a clearly better multiline form.
5. Prefer f-strings over older string-formatting styles.
6. Prefer explicit loops over dense comprehensions when the loop is clearer.
7. Leave two blank lines between member functions in classes in this repository.
8. Do not enforce any hard file-length limit. Split a Python module **only** when it has grown large *and* its contents naturally decompose into independent responsibilities that read better as separate files. If an oversized module is already cohesive (one type, one concern), leave it alone.
9. When a module exposes a reusable public type, document the clean interface at the module/class level: construction, injected dependencies, state-mutation methods, public API surface, and the black-box behavior callers can rely on.

## Checklist

1. Consider splitting a touched Python file only when it is both large *and* the contents fall into clearly separable responsibilities. Don't split a cohesive module just because it is long.
2. When splitting is justified, peel helpers, tree/UI logic, and state reconciliation out into separate modules.
3. Rewrite overly clever Python into direct control flow.
4. Add or refresh module/class docstrings when the code defines a reusable public type.
5. Re-run the repository Python syntax check after the refactor.
