# tgdb Pipe Protocol

tgdb communicates with GDB through a single unidirectional pipe (GDB → tgdb).
The pipe carries both lightweight event notifications and bulk debugger-state
payloads in a compact binary frame format.

## Pipe setup

Before forking GDB, tgdb creates an `os.pipe()` pair and passes the write-end
fd to the GDB process.  At GDB startup, `tgdb_pysetup.py` is sourced and
`register_pipe_fd(fd)` wires GDB Python event handlers and collection
functions to that fd.

The read-end fd is set to non-blocking mode and registered with the asyncio
event loop via `loop.add_reader()`.  The kernel pipe buffer is enlarged to
1 MB with `fcntl(fd, F_SETPIPE_SZ, 1048576)`.

## Frame format

The **tag byte** alone determines how each frame is parsed.  There are
three frame categories:

```
No-payload tags        [tag]                                        (1 byte)
Fixed-payload tags     [tag][payload]                               (1 + N bytes)
Variable-length tags   [tag][ctl][7-byte BE payload_length][payload] (9 + payload_length bytes)
```

### No-payload tags (1 byte total)

These tags carry no data beyond the tag itself.

| Tag | ASCII | Event              | Description                                        |
|-----|-------|--------------------|----------------------------------------------------|
| `P` | 0x50  | `before_prompt`    | GDB is about to print its prompt — refresh frame   |
| `O` | 0x4F  | `new_objfile`      | A shared library was loaded                        |
| `F` | 0x46  | `free_objfile`     | A shared library was unloaded                      |
| `C` | 0x43  | `clear_objfiles`   | Program space was wiped (e.g. `kill`)              |
| `X` | 0x58  | `gdb_exiting`      | GDB's main loop is tearing down                    |

### Fixed-payload tags (tag + known-size payload)

The payload size is implied by the tag — no length field is needed.

| Tag | ASCII | Size | Event              | Payload format                                   |
|-----|-------|------|--------------------|--------------------------------------------------|
| `R` | 0x52  | 4    | `register_changed` | 4-byte signed big-endian register number (-1 = all) |
| `I` | 0x49  | 1    | `inferior_call`    | `0x00` = pre-call, `0x01` = post-call            |

### Variable-length tags (tag + ctl + 7-byte length + payload)

These tags carry variable-length payloads with a **control byte** for
encoding flags and a **7-byte big-endian** payload length (max ~128 PB).

```
┌──────────┬─────────┬──────────────────────────┬───────────────────────┐
│ tag (1B) │ ctl(1B) │ payload_length (7B, BE)  │ payload               │
└──────────┴─────────┴──────────────────────────┴───────────────────────┘
```

**Control byte bit flags:**

| Bit | Mask | Name             | Description                                    |
|-----|------|------------------|------------------------------------------------|
| 0   | 0x01 | `CTL_COMPRESSED` | Payload is zlib-compressed                     |
| 1–7 |      |                  | Reserved (must be 0)                           |

Compression is applied automatically on the sender side when the raw
payload is ≥ 64 bytes.  The receiver checks `CTL_COMPRESSED` and
decompresses if set, regardless of size.

| Tag | ASCII | Payload type | Description                                     |
|-----|-------|--------------|-------------------------------------------------|
| `l` | 0x6C  | JSON         | Local variables (`$_tgdb_RSVD_collect_locals()`)    |
| `s` | 0x73  | JSON         | Stack frames (`$_tgdb_RSVD_collect_stack()`)        |
| `r` | 0x72  | JSON         | Register values (`$_tgdb_RSVD_collect_registers()`) |
| `f` | 0x66  | JSON         | Current frame info (`$_tgdb_RSVD_collect_frame_info()`) |
| `b` | 0x62  | JSON         | Breakpoint list (`$_tgdb_RSVD_collect_breakpoints()`) |
| `D` | 0x44  | Raw UTF-8    | Diagnostic log message from GDB Python          |

## Why thread info is not on the pipe

Thread info is fetched via MI `-thread-info` instead of a pipe convenience
function.  An earlier version used the pipe with `_collect_threads()`, but it
was reverted for the following reasons:

1. **The GDB Python API has no read-only thread iteration.**  To read another
   thread's topmost frame you must call `thread.switch()`, which changes GDB's
   selected-thread *and* selected-frame to frame #0 of that thread.

2. **Save/restore is not safe under concurrent MI.**  Even if you save and
   restore the original thread and frame, other MI commands queued on the same
   channel (e.g. `collect_locals`) can execute between the switch and the
   restore, observing the wrong frame.  This manifested as the locals pane
   showing frame #0 variables after `up`/`down` navigation.

3. **MI `-thread-info` is read-only at the C level.**  It iterates threads and
   returns per-thread frame info without touching the selected context.  The
   response size is modest (tens to hundreds of threads), so MI overhead is
   acceptable — unlike locals or registers where payloads can be megabytes.

The `_collect_threads()` implementation has been removed.  If a future GDB
version adds a read-only Python API for cross-thread frame access, thread info
can be moved to the pipe using a new bulk data tag.

## JSON payload schemas

### `l` — Local variables

Array of objects, one per local variable/argument in the current frame.

```json
[
  {
    "name": "x",
    "value": "42",
    "type": "int",
    "is_arg": false,
    "is_reference": false,
    "ref_kind": null,
    "line": 10,
    "addr": "0x7fffffffe4ac",
    "depth": 0,
    "is_shadowed": false,
    "scope_start": "0x555555555180"
  }
]
```

| Field         | Type           | Description                                          |
|---------------|----------------|------------------------------------------------------|
| `name`        | string         | Variable name                                        |
| `value`       | string         | Formatted value string                               |
| `type`        | string         | C/C++ type name                                      |
| `is_arg`      | bool           | True if function argument                            |
| `is_reference`| bool           | True if lvalue or rvalue reference                   |
| `ref_kind`    | string \| null | `"lvalue (&)"`, `"rvalue (&&)"`, or `null`           |
| `line`        | int            | Declaration line number                              |
| `addr`        | string         | Memory address or `"register"`                       |
| `depth`       | int            | Block nesting depth (0 = innermost)                  |
| `is_shadowed` | bool           | True if shadowed by a variable in a deeper scope     |
| `scope_start` | string         | Hex address of enclosing block start                 |

### `s` — Stack frames

Array of frame objects, ordered from newest (level 0) to oldest.

```json
[
  {
    "level": 0,
    "func": "main",
    "addr": "0x555555555180",
    "file": "test.cpp",
    "fullname": "/home/user/test.cpp",
    "line": 42
  }
]
```

| Field      | Type   | Description                               |
|------------|--------|-------------------------------------------|
| `level`    | int    | Frame level (0 = newest)                  |
| `func`     | string | Function name (empty if unknown)          |
| `addr`     | string | Hex program counter address               |
| `file`     | string | Source file basename                       |
| `fullname` | string | Absolute source file path                 |
| `line`     | int    | Source line number (0 if unknown)          |

### `r` — Register values

Array of register objects.

```json
[
  {
    "name": "rax",
    "value": "0x42",
    "number": 0
  }
]
```

| Field    | Type   | Description                          |
|----------|--------|--------------------------------------|
| `name`   | string | Register name                        |
| `value`  | string | Hex-formatted value                  |
| `number` | int    | Architecture register number         |

### `f` — Current frame info

Single object describing the currently selected frame.  Empty object `{}`
signals no frame is available (e.g. inferior not started).

```json
{
  "level": 0,
  "func": "main",
  "addr": "0x555555555180",
  "file": "test.cpp",
  "fullname": "/home/user/test.cpp",
  "line": 42,
  "arch": "i386:x86-64"
}
```

| Field      | Type   | Description                                     |
|------------|--------|-------------------------------------------------|
| `level`    | int    | Frame level (always 0 for selected frame)       |
| `func`     | string | Function name                                   |
| `addr`     | string | Hex program counter address                     |
| `file`     | string | Source file basename                             |
| `fullname` | string | Absolute source file path                       |
| `line`     | int    | Source line number                               |
| `arch`     | string | Architecture name (e.g. `"i386:x86-64"`)        |

### `b` — Breakpoint list

Array of breakpoint objects.

```json
[
  {
    "number": 1,
    "file": "test.cpp",
    "fullname": "/home/user/test.cpp",
    "line": 42,
    "addr": "",
    "enabled": true,
    "temporary": false,
    "location": "main"
  }
]
```

| Field       | Type   | Description                                        |
|-------------|--------|----------------------------------------------------|
| `number`    | int    | Breakpoint number                                  |
| `file`      | string | Source file basename (empty if unresolved)          |
| `fullname`  | string | Absolute source file path (empty if unresolved)    |
| `line`      | int    | Source line number (0 if unresolved)                |
| `addr`      | string | Address (empty — reserved for future use)          |
| `enabled`   | bool   | Whether the breakpoint is enabled                  |
| `temporary` | bool   | Whether this is a temporary breakpoint             |
| `location`  | string | Location expression as passed to GDB               |

## Event coalescing

The reader batches frames received in a single `read()` cycle and coalesces
repeated events before dispatching callbacks:

- **`P` (before_prompt)**: at most one `on_cli_prompt` callback per read cycle.
- **`O`/`F`/`C` (objfile events)**: at most one `on_objfiles_changed` callback.
- **`R` (register_changed)**: deduplicated by register number; if `-1` (all)
  is present, only `-1` is dispatched.

`I` (inferior_call) and `X` (gdb_exiting) fire immediately without coalescing.

`D` (diagnostic log) fires immediately, writing to the tgdb log at DEBUG level.

Variable-length data tags (`l`, `s`, `r`, `f`, `b`) are dispatched immediately in
order of arrival.

## Data collection flow

Bulk data is collected by GDB-side Python functions registered as GDB
convenience functions (e.g. `$_tgdb_RSVD_collect_locals()`).  tgdb invokes
them via `-data-evaluate-expression` on the MI channel:

```
-data-evaluate-expression "$_tgdb_RSVD_collect_locals()"
```

The convenience function collects data using GDB's Python API, serializes it
as JSON, zlib-compresses it, writes the framed payload to the pipe, and
returns `"ok"` as the MI result.  The actual data arrives asynchronously
through the pipe, decoupling bulk data transfer from the MI command stream.

## Implementation files

| File                               | Role                                           |
|------------------------------------|-------------------------------------------------|
| `tgdb/tgdb_pysetup.py`            | GDB-side: pipe registration, event handlers, collection functions, convenience function classes |
| `tgdb/gdb_controller/pipe_data.py` | tgdb-side: `PipeDataMixin` — frame parser, event coalescing, data dispatch and handlers |
| `tgdb/gdb_controller/controller.py`| Pipe creation (`os.pipe()`), fd lifecycle, asyncio reader registration |
| `tgdb/gdb_controller/requests.py`  | MI request helpers that trigger pipe-based collection |
