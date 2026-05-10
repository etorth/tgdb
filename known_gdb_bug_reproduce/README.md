# `known_gdb_bug_reproduce/`

Self-contained reproducers for the GDB defects catalogued in
[`../docs/known_gdb_bug.md`](../docs/known_gdb_bug.md).

`## Bug N` in the doc maps to `bug_N.py` in this directory.

## Running

Each script depends only on `gdb`, `g++`, and the Python standard library.
Run from the repo root:

```sh
python3 known_gdb_bug_reproduce/bug_1.py
python3 known_gdb_bug_reproduce/bug_2.py
```

## Exit codes

| Code | Meaning                                                    |
|------|------------------------------------------------------------|
| 0    | Bug reproduced — the local GDB build still has it.         |
| 1    | Bug did **not** reproduce — likely fixed upstream / build differs. |
| 2    | `gdb` or `g++` not in `PATH`.                              |
| 3    | Setup diverged from the hypothesis (printer behaviour, missing children, etc.) — the reproducer could not even reach the trigger step.  Treat as a separate investigation. |

## Adding a new reproducer

1. Add a `## Bug N: ...` section to `../docs/known_gdb_bug.md` following
   the existing field skeleton (Symptom / Trigger / Crash signature /
   Affected versions / tgdb workaround / Reproducer).
2. Create `bug_N.py` here.  Copy the structure of an existing `bug_*.py`
   for the MI-driver scaffolding (`MISession`, tokenised `cmd`,
   async `wait_for_stop`).
3. Reference the script from the doc's Reproducer subsection.
