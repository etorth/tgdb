# python-style-format

Apply this skill after Python code changes in this repository.

## Intent

Keep Python code explicit, readable, and split by responsibility.

## Rules

1. Follow PEP 8, but do not enforce a hard line-length limit.
2. Break lines only when the broken form is easier to understand.
3. Prefer f-strings over older string-formatting styles.
4. Prefer explicit loops over dense comprehensions when the loop is clearer.
5. Leave two blank lines between member functions in classes in this repository.
6. Keep Python files around 500 lines when practical by splitting large modules.
7. When a module exposes a reusable public type, document the clean interface at the module/class level: construction, injected dependencies, state-mutation methods, public API surface, and the black-box behavior callers can rely on.

## Checklist

1. Check whether the touched Python file is too large.
2. Split helpers, tree/UI logic, and state reconciliation into separate modules when that improves readability.
3. Rewrite overly clever Python into direct control flow.
4. Add or refresh module/class docstrings when the code defines a reusable public type.
5. Re-run the repository Python syntax check after the refactor.
