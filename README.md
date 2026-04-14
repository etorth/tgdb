# [Homepage](https://tgdb-proj.github.io/)
# tgdb — Vi-like TUI front-end for GDB

`tgdb` is a terminal-based front-end for GDB, reimplemented in Python using
[Textual](https://textual.textualize.io/) for the TUI and
[Pygments](https://pygments.org/) for syntax highlighting.
It aims for **100% compatibility** with [cgdb](https://cgdb.github.io/) in
terms of keybindings, configuration, and behaviour.

---

## Features

- **Split-screen TUI**: syntax-highlighted source window above, GDB console below
- **Vi-like keybindings** in the source window
- **Syntax highlighting** for C, C++, Python, Rust, Go, Ada, Fortran, and more (via Pygments)
- **Visual breakpoint management**: set/delete/enable/disable with `Space`
- **Executing line indicator**: `shortarrow`, `longarrow`, `highlight`, or `block` styles
- **Regex search** in source window (`/`, `?`, `n`, `N`) with optional `hlsearch`
- **Marks** (`m[a-z/A-Z]` / `'[a-z/A-Z]`) — local and global
- **File dialog** (`o`) to browse and select source files
- **GDB scrollback buffer** with configurable size and vi-style scroll mode
- **ANSI colour support** in GDB output window
- **`~/.config/tgdb/tgdbrc`** config file (`XDG_CONFIG_HOME/tgdb/tgdbrc`)
- **Numeric prefix** for movement: `[N]j`, `[N]k`, `[N]G` (e.g. `20j` = down 20 lines)
- **Horizontal vim navigation**: `0` = beginning of line, `^` = first char, `$` = end of line
- **Goto line**: `:N` jumps to line N in the source window (`:+N` / `:-N` for relative)
- **Command history**: persistent across sessions, `!!` reruns last command, `!N` reruns entry N
- **Comments in command mode**: lines starting with `#` are no-ops (useful in rc files and history)
- **Async Python scripting**: `:python await foo()`, heredoc `:python << EOF`, top-level `await`

---

## Installation

```bash
pip install -e .
# or
pip install textual pygments ptyprocess pyte
python -m tgdb
```

---

## Usage

```
tgdb [OPTIONS] [PROGRAM [CORE|PID]]

Options:
  -d, --debugger DEBUGGER   Path to GDB executable (default: gdb)
  -r, --rcfile FILE         Configuration file (default: ~/.config/tgdb/tgdbrc)
                            Use --rcfile NONE (uppercase) to skip loading
  --args                    Pass remaining args as program + arguments to GDB
  --cd DIR                  Change to DIR before starting GDB
```

Examples:
```bash
tgdb myprogram
tgdb -d /usr/bin/gdb myprogram
tgdb --args myprogram arg1 arg2
tgdb myprogram core
```

---

## Keybindings

### CGDB mode (source window, default mode)

| Key | Action |
|-----|--------|
| `ESC` | Stay in / return to CGDB mode |
| `i` | Switch to GDB mode |
| `s` | Switch to GDB scroll mode |
| `[N]j` / `[N]↓` | Move cursor down N lines (default 1) |
| `[N]k` / `[N]↑` | Move cursor up N lines (default 1) |
| `h` / `←` | Move left (horizontal scroll) |
| `l` / `→` | Move right (horizontal scroll) |
| `0` | Go to beginning of line (reset horizontal scroll) |
| `^` | Go to first visible char (same as `0`) |
| `$` | Go to end of current line |
| `Ctrl-f` / `PageDn` | Page down |
| `Ctrl-b` / `PageUp` | Page up |
| `Ctrl-d` | Half page down |
| `Ctrl-u` | Half page up |
| `gg` | Go to top |
| `[N]G` | Go to line N (or bottom if no prefix) |
| `H` / `M` / `L` | Screen top / middle / bottom |
| `/` | Forward search |
| `?` | Backward search |
| `n` / `N` | Next / previous search match |
| `Space` | Toggle breakpoint at current line |
| `t` | Set temporary breakpoint |
| `o` | Open file dialog |
| `m[a-z/A-Z]` | Set mark |
| `'[a-z/A-Z]` | Jump to mark |
| `''` | Jump to last jump location |
| `'.` | Jump to executing line |
| `Ctrl-W` | Toggle split orientation |
| `-` / `=` | Shrink / grow source window by 1 |
| `_` / `+` | Shrink / grow source window by 25% |
| `Ctrl-l` | Redraw screen |
| `F5` | `run` |
| `F6` | `continue` |
| `F7` | `finish` |
| `F8` | `next` |
| `F10` | `step` |
| `:` | Enter command mode |

### GDB mode (GDB console)

| Key | Action |
|-----|--------|
| `ESC` | Switch to CGDB mode |
| `PageUp` | Enter scroll mode |
| `↑` / `↓` | Command history |
| `Ctrl-C` | Interrupt GDB |
| `Enter` | Send command to GDB |

### Scroll mode (GDB console scrollback)

| Key | Action |
|-----|--------|
| `ESC` / `q` / `i` / `Enter` | Exit scroll mode |
| `j` / `↓` | Scroll down |
| `k` / `↑` | Scroll up |
| `PageUp` / `PageDn` | Page up / down |
| `Ctrl-u` / `Ctrl-d` | Half page |
| `gg` / `G` | Beginning / end of buffer |
| `/` / `?` / `n` / `N` | Search |

### File dialog

| Key | Action |
|-----|--------|
| `q` / `ESC` | Close dialog |
| `j` / `k` | Move down / up |
| `Enter` | Open selected file |
| `/` / `?` / `n` / `N` | Search files |

---

## Configuration

tgdb reads `~/.config/tgdb/tgdbrc` (or `$XDG_CONFIG_HOME/tgdb/tgdbrc`) on
startup.  Commands can also be typed interactively via `:`.

Use `--rcfile NONE` (uppercase) to skip loading the rc file.

### Comments in command mode

Any line beginning with optional whitespace then `#` is a comment and is ignored:

```
# This line is a comment
set hlsearch     # NOT a comment — '#' only counts at line start
```

Comments are valid in rc files, in `:source`-d files, and in interactive
command mode.  They are also recorded in history (the session-delimiter line
`# tgdb begins ...` uses this mechanism).

### `:set` options

| Option | Abbrev | Default | Description |
|--------|--------|---------|-------------|
| `autosourcereload` | `asr` | on | Auto-reload changed source files |
| `cgdbmodekey=KEY` | — | `ESC` | Key to enter CGDB mode |
| `color` | — | on | Enable colour support |
| `debugwincolor` | `dwc` | on | Show ANSI colours in GDB window |
| `disasm` | `dis` | off | Show disassembly |
| `executinglinedisplay=STYLE` | `eld` | `longarrow` | `shortarrow`\|`longarrow`\|`highlight`\|`block` |
| `history=N` | — | 1024 | Command history buffer size (0 = disabled) |
| `hlsearch` | `hls` | off | Highlight all search matches |
| `ignorecase` | `ic` | off | Case-insensitive search |
| `scrollbackbuffersize=N` | `sbbs` | 10000 | GDB scrollback lines |
| `selectedlinedisplay=STYLE` | `sld` | `block` | Same styles as above |
| `showmarks` | — | on | Show marks in gutter |
| `showdebugcommands` | `sdc` | off | Show GDB commands sent |
| `tabstop=N` | `ts` | 8 | Tab width |
| `timeout` | `to` | on | Timeout on key maps |
| `timeoutlen=N` | `tm` | 1000 | Map timeout (ms) |
| `ttimeout` | — | on | Timeout on key codes |
| `ttimeoutlen=N` | `ttm` | 100 | Key code timeout (ms) |
| `winminheight=N` | `wmh` | 0 | Minimum window height |
| `winminwidth=N` | `wmw` | 0 | Minimum window width |
| `winsplit=STYLE` | — | `even` | `src_full`\|`src_big`\|`even`\|`gdb_big`\|`gdb_full` |
| `winsplitorientation=STYLE` | `wso` | `vertical` | `horizontal`\|`vertical` |
| `wrapscan` | `ws` | on | Search wraps around end of file |

Setting `history=0` disables history (buffer cleared immediately).  Use a very
large number (e.g. `set history=9999999`) for effectively unlimited history.

### History commands

| Command | Description |
|---------|-------------|
| `:history` | List all recorded commands with index numbers |
| `:save history` | Save history buffer to disk immediately |
| `:save history FILE` | Save history buffer to FILE |
| `!!` | Re-run the last non-comment history entry |
| `!N` | Re-run history entry number N |

History is loaded from `~/.local/state/tgdb/history` at startup.
A session delimiter comment (`# tgdb begins YYYY-MM-DD HH:MM:SS`) is
automatically prepended.  Commands from the rc file are **not** recorded;
only interactive commands are saved.  History is written to disk on exit.

### `:highlight` — customise colours

```
:highlight GROUP ctermfg=COLOR ctermbg=COLOR cterm=ATTRS
```

Groups: `Statement`, `Type`, `Constant`, `Comment`, `PreProc`, `Normal`,
`StatusLine`, `Search`, `IncSearch`, `SelectedLineArrow`, `ExecutingLineArrow`,
`SelectedLineHighlight`, `ExecutingLineHighlight`, `SelectedLineBlock`,
`ExecutingLineBlock`, `Breakpoint`, `DisabledBreakpoint`, `Logo`, `Mark`, etc.

Colours: `Black`, `DarkBlue`, `DarkGreen`, `DarkCyan`, `DarkRed`, `DarkMagenta`,
`Brown`/`DarkYellow`, `LightGray`/`Gray`, `DarkGray`, `Blue`/`LightBlue`,
`Green`/`LightGreen`, `Cyan`/`LightCyan`, `Red`/`LightRed`,
`Magenta`/`LightMagenta`, `Yellow`/`LightYellow`, `White`.

Attributes: `bold`, `underline`, `reverse`/`inverse`, `standout`, `blink`, `dim`, `normal`/`NONE`.

### `:map` / `:imap` — key mappings

```
:map <F7> :step<Enter>
:map <F8> :next<Enter>
:imap <C-x> :quit<Enter>
:unmap <F7>
:iunmap <C-x>
```

### Example `~/.config/tgdb/tgdbrc`

```
set tabstop=4
set hlsearch
set autosourcereload
set executinglinedisplay=longarrow
set selectedlinedisplay=block
set scrollbackbuffersize=50000
set winsplit=even
highlight Statement ctermfg=Yellow cterm=bold
highlight Comment ctermfg=DarkGray
highlight Breakpoint ctermfg=Red cterm=bold
map <F5> :run<Enter>
map <F6> :continue<Enter>
map <F7> :finish<Enter>
map <F8> :next<Enter>
map <F10> :step<Enter>
```

---

## Architecture

```
tgdb/
├── __main__.py          CLI entry point (argparse, launches app)
├── app/                 Main application package — TGDBApp + workspace tree
├── gdb_controller/      GDB bridge package — controller, MI parser, callbacks
├── source_widget/       Source code viewer package (SourceView + internal helpers)
├── gdb_widget/          GDB console package — pane, screen, scroll mode
├── command_line_bar/    Command/status bar package
├── context_menu/        Cascading workspace context-menu package
├── status_bar.py        Status bar widget
├── file_dialog/         File picker dialog package
├── config/             Configuration package (cgdbrc parser + helpers)
├── highlight_groups.py  Highlight group table and colour management
└── key_mapper.py        Key mapping with trie + timeout (KUI)
```

### How it works

1. **Startup**: `TGDBApp` reads `~/.cgdb/cgdbrc`, spawns GDB via `ptyprocess`
   with `--interpreter=mi2`.
2. **GDB I/O**: An asyncio task reads GDB output from the PTY.
   - Lines starting with `~` (console stream) → GDB widget
   - Lines starting with `*` or `=` (async events) → decoded for source
     position, breakpoints, etc.
3. **Source view**: When GDB stops, the `*stopped` MI event provides the
   current file and line. The source widget loads the file (if needed) and
   positions the executing-line arrow.
4. **Breakpoints**: The space bar sends `-break-insert file:line` via MI.
   Breakpoint events update the gutter markers.
5. **Key mapping**: All keys pass through `KeyMapper` which resolves user
   maps (`:map`/`:imap`) before dispatching.

---

## Differences from cgdb

- Uses **Textual** (Python TUI framework) instead of ncurses directly
- Uses **Pygments** for syntax highlighting (supports many more languages
  than cgdb's built-in flex lexers)
- GDB/MI parsing is done in pure Python (no C++ gdbwire library)
- Readline integration: basic history via up/down arrows (no libreadline)
- The `--tty` / `Ctrl-T` separate TTY feature is not yet implemented

gdb
