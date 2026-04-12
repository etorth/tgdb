---
applyTo: "**/*.py"
---

# python-style-format

Apply these rules after every Python code change in this repository.

- Keep Python modules around **500 lines** when practical. If a file grows much beyond that, split it by responsibility instead of extending the monolith.
- Follow **PEP 8** for naming, spacing, and structure, but **do not** treat line length as a hard rule. Break a line only when the broken form is genuinely easier to read.
- Prefer **f-strings** over `%` formatting or logger argument tuples when writing or updating string formatting.
- Avoid dense or overly clever Python constructs when the logic is non-trivial. In particular, prefer explicit `for` loops over multi-condition comprehensions when the loop is easier to read.
- Leave **two blank lines between member functions** in classes in this codebase.
- Prefer direct, readable control flow over compact “pythonic” one-liners when there is any meaningful branching or filtering.
- When splitting a large Python file, preserve behavior first, then improve names and local structure without changing unrelated logic.
