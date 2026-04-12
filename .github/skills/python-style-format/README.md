# python-style-format

Use this repository-local skill after Python code changes.

## Goal

Keep Python code readable, explicit, and easy to maintain in this repository.

## Rules

1. Follow PEP 8, but do **not** enforce a hard line-length limit.
2. Break lines only when the broken form is easier to read.
3. Prefer **f-strings** for string formatting.
4. Avoid dense comprehensions and other “too pythonic” constructs when an explicit loop is clearer.
5. Leave **two blank lines** between member functions in classes in this repository.
6. Keep Python files around **500 lines** when practical by splitting large modules by responsibility.

## Refactoring checklist

1. Check whether the touched Python file is getting too large.
2. Split helpers, state reconciliation, and UI/tree logic into separate modules when that improves readability.
3. Rewrite hard-to-scan comprehensions as explicit loops.
4. Rewrite old-style logging/string formatting with f-strings when the code is being touched.
5. Re-run the repository Python syntax check after the refactor.
