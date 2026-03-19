# Copilot Instructions for `tgdb`

## Build, test, and validation commands

- There is no committed `pytest` / `ruff` / `mypy` test or lint suite in this repository. The current validation workflow is syntax-checking plus manual compatibility checks against `cgdb`.
- Install the package and runtime dependencies:
  ```bash
  pip install -e .
  ```
- Sanity-check the CLI entry point:
  ```bash
  python -m tgdb --help
  ```
- Syntax-check the whole package:
  ```bash
  python -m py_compile tgdb/*.py
  ```
- Syntax-check one module while iterating on a focused change:
  ```bash
  python -m py_compile tgdb/source_widget.py
  ```
- Build the checked-in debug fixture used for manual regression testing:
  ```bash
  g++-15 -std=c++26 test/test_cpp26.cpp -g -o /tmp/a.out
  ```
- Run `tgdb` against that fixture:
  ```bash
  python -m tgdb /tmp/a.out
  ```
- Run the reference implementation for side-by-side behavior checks:
  ```bash
  cgdb /tmp/a.out
  ```

## High-level architecture

- `tgdb/__main__.py` is a thin cgdb-compatible CLI wrapper. It parses `-d`, `-w`, `-r`, `--args`, and `--cd`, then launches `TGDBApp`.
- `tgdb/app.py` is the orchestration layer. It composes the Textual widgets, owns mode and split state, registers `:` commands, and translates widget messages into debugger actions.
- `tgdb/gdb_controller.py` is the debugger bridge. It uses **two PTYs**:
  - the primary PTY is the normal GDB console stream, forwarded as raw bytes to the bottom pane;
  - the secondary PTY is a `new-ui mi ...` channel used for structured MI records such as stopped frames, source files, and breakpoints.
- `tgdb/gdb_widget.py` is a terminal emulator wrapper around `pyte`. It renders the raw console PTY, keeps scrollback, and implements cgdb-style GDB scroll mode and search.
- `tgdb/source_widget.py` is the source pane. It uses Pygments for tokenization, but its rendering rules are compatibility-driven: executing/selected line styles, marks, file positions, horizontal scrolling, wide-character clipping, and long-line truncation are all custom.
- `tgdb/status_bar.py` is not just display chrome; it owns `:` command entry, `/` and `?` prompts, focus markers, and drag-resize interaction for horizontal splits.
- `tgdb/file_dialog.py` is a full-screen source-file picker with its own search/navigation model. It is meant to mirror cgdb’s dialog behavior rather than act like a generic list widget.
- `tgdb/config.py`, `tgdb/highlight_groups.py`, and `tgdb/key_mapper.py` together implement cgdb-style config parsing:
  - `ConfigParser` reads `~/.cgdb/cgdbrc` or `$CGDB_DIR/cgdbrc`;
  - `HighlightGroups` keeps cgdb-compatible group names and color semantics;
  - `KeyMapper` resolves `:map` / `:imap` expansions using a trie with timeout behavior.

## Key conventions

- This repo optimizes for **cgdb compatibility**, not Textual defaults. When framework behavior and cgdb behavior differ, preserve or restore the cgdb-compatible behavior.
- Prefer the widget-message pattern already used across the codebase:
  - widgets post semantic `Message` objects;
  - `TGDBApp` performs focus changes, GDB I/O, file loads, split updates, and mode transitions.
- Keep debugger I/O split cleanly:
  - user keystrokes go to the primary GDB PTY;
  - structured debugger state comes from the MI PTY;
  - UI updates from controller callbacks should be bounced back onto the Textual app via the existing `call_later(...)` wiring in `TGDBApp`.
- Keep configuration and highlight behavior in **cgdb vocabulary**. The option names, aliases, highlight groups, and command semantics are intentionally modeled after cgdb, not renamed to match Textual/Rich terminology.
- Source-pane horizontal scrolling is measured in **display cells**, not Python codepoints. Wide-character clipping must preserve terminal-cell alignment; if only half of a wide character remains after scrolling or truncation, render that visible half as `?`.
- Long source lines should be **cropped to the pane width**, not wrapped onto extra rows.
- File dialog ordering is deliberate: dedupe entries, skip missing files unless they are special `*` entries, then sort plain relative paths before `./` paths before absolute paths.
- Split handling is deliberate cgdb math, not a generic proportional layout. `winsplit` presets and resize keys map to cgdb-style `window_shift` behavior over the full terminal axis, and zero-sized panes are hidden explicitly.
- `:help` should prefer the cgdb manual text (`cgdb.txt`) when available. `:logo` should keep **tgdb’s custom logo**; do not replace it with cgdb artwork.
- `test/test_cpp26.cpp` is the main manual compatibility fixture. It intentionally contains C++26 syntax, long lines, CJK text, emoji, and other rendering edge cases. Use it whenever you touch source rendering, scrolling, file loading, or split/layout behavior.
- If README text and runtime behavior disagree, verify against the current code before treating the README as the source of truth.
