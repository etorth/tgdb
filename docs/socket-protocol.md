# tgdb Socket Protocol

tgdb communicates with GDB through an `AF_UNIX` socketpair (bidirectional).
The socket carries both lightweight event notifications and bulk debugger-state
payloads in a compact binary frame format.  The socket is bidirectional: GDB
writes data/events to tgdb, and tgdb can write cancel tokens back to GDB.

## Socket setup

Before forking GDB, tgdb creates a `socket.socketpair(AF_UNIX, SOCK_STREAM)`
and passes one end's fd to the GDB process.  At GDB startup, `tgdb_pysetup.py`
is sourced and `_tgdb_RSVD_register_socket_fd(fd)` wires GDB Python event handlers and
collection functions to that fd.

The tgdb-side fd is set to non-blocking mode and registered with the asyncio
event loop via `loop.add_reader()`.  The socket buffers are enlarged to
1 MB with `setsockopt(SOL_SOCKET, SO_RCVBUF/SO_SNDBUF, 1048576)`.

## Frame format

The **tag byte** alone determines how each frame is parsed.  There are
four frame categories:

```
No-payload tags        [tag]                                        (1 byte)
Fixed-payload tags     [tag][payload]                               (1 + N bytes)
Varint-payload tags    [tag][unsigned varint]                       (1 + 1–N bytes)
Variable-length tags   [tag][ctl][payload_length varint][payload]   (2 + varint + payload bytes)
```

### Varint encoding (unsigned LEB128)

Integers are encoded using unsigned LEB128 (Little Endian Base 128).
Each byte carries 7 data bits; the MSB is a continuation flag:

- **MSB = 0**: this is the last byte — take the lower 7 bits.
- **MSB = 1**: take the lower 7 bits, shift, and read the next byte.

Examples: `0` → `0x00` (1 byte), `127` → `0x7F` (1 byte),
`128` → `0x80 0x01` (2 bytes), `300` → `0xAC 0x02` (2 bytes).

All integers in the protocol are unsigned.  The register-changed tag
encodes `regnum + 1` so that the sentinel -1 ("all") maps to 0.

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
| `I` | 0x49  | 1    | `inferior_call`    | `0x00` = pre-call, `0x01` = post-call            |

### Varint-payload tags (tag + unsigned varint)

The payload is a single unsigned LEB128 varint.

| Tag | ASCII | Event              | Payload format                                          |
|-----|-------|--------------------|---------------------------------------------------------|
| `R` | 0x52  | `register_changed` | `regnum + 1` as unsigned varint (0 = all registers)     |

### Variable-length tags (tag + ctl + varint length + payload)

These tags carry variable-length payloads with a **control byte** for
encoding flags and a **varint** payload length (unsigned LEB128).

```
┌──────────┬─────────┬────────────────────────┬───────────────────────┐
│ tag (1B) │ ctl(1B) │ payload_length (varint) │ payload               │
└──────────┴─────────┴────────────────────────┴───────────────────────┘
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

#### MI token in data payloads

Data tags (`l`, `s`, `r`, `f`, `b`) embed a varint **MI token** at the
start of their payload so the tgdb side can correlate the data with the
MI command that triggered the collection.  The log tag (`D`) does not
carry a token.

After decompression (if applicable), the payload layout for data tags is:

```
┌─────────────────────┬──────────────────────────────┐
│ mi_token (varint)   │ JSON bytes                   │
└─────────────────────┴──────────────────────────────┘
```

A token of `0` means no MI correlation (legacy / non-convenience path).

## Why thread info is not on the socket

Thread info is fetched via MI `-thread-info` instead of a socket convenience
function.  An earlier version used the socket with `_collect_threads()`, but it
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
can be moved to the socket using a new bulk data tag.

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
convenience functions (e.g. `$_tgdb_RSVD_collect_locals(token)`).  tgdb
invokes them via `-data-evaluate-expression` on the MI channel, passing
the **MI command token** as the integer argument — the same token that
prefixes the MI command on the wire:

```
42-data-evaluate-expression "$_tgdb_RSVD_collect_locals(42)"
```

The convenience function collects data using GDB's Python API, serializes it
as JSON, zlib-compresses it, writes the framed payload to the socket, and
returns `"done"` as the MI result.  The actual data arrives asynchronously
through the socket, decoupling bulk data transfer from the MI command stream.

If the function detects that its token has been cancelled (see
**Cancellation** below), it returns `"cancelled"` without sending any data.

## Cancellation

The socket is bidirectional.  In the **tgdb→GDB** direction, tgdb writes
varint-encoded unsigned integers (tokens) to request cancellation of
pending or in-progress convenience function calls.  The cancel token is
the same MI command token that prefixed the original request — a single
counter serves both purposes.

### Wire format (tgdb→GDB)

```
┌────────────────────────┐
│ mi_token (varint LEB)  │
└────────────────────────┘
```

Tokens are written directly to the socket with no framing — the GDB-side
reader thread reads in a loop and decodes varints from the byte stream.

### GDB-side cancel reader

`_tgdb_RSVD_register_socket_fd()` starts a daemon thread (`tgdb-cancel-reader`) that
reads cancel tokens from the socket and adds them to a thread-safe
`set[int]` protected by a `threading.Lock`.

### Convenience function integration

Each convenience function receives a cancel token as its first argument
(default `0` = no cancellation).  At key checkpoints the function calls
`_is_cancelled(token)` to test the set.  When cancelled:

1. The function calls `_finish_token(token)` to remove the token from the set.
2. Returns `"cancelled"` as the MI result — no socket data is sent.

The tgdb side detects `"cancelled"` in the MI expression value and resolves
the Future immediately without waiting for socket data.

When completed normally, the function also calls `_finish_token(token)` to
clean up.

### Cancel checkpoints

| Function            | Checkpoint locations                                      |
|---------------------|-----------------------------------------------------------|
| `_collect_locals`   | Before block walk, at each block depth, before dedup/send |
| `_collect_stack`    | Before frame walk, every 50 frames                        |
| `_collect_registers`| Before register iteration                                 |
| `_collect_frame_info`| Not checked (always fast)                                |
| `_collect_breakpoints`| Not checked (always fast)                               |

### Cancellation semantics

- **Best-effort**: a fast function may complete before the cancel token
  arrives — that is expected and harmless.
- **Token 0** means "no cancellation support" and is never checked.
- Since GDB processes convenience functions serially, multiple cancel
  tokens can accumulate in the set before any function checks them.
- The tgdb side stores the latest cancel token per request type
  (`_locals_cancel_token`, `_stack_cancel_token`, etc.) so it knows
  which token to cancel when superseding an in-flight request.

## Two-part Future completion

For MI commands that invoke convenience functions (`expect_socket=True`),
the `mi_command_async` Future waits for **both** the MI response and the
socket data payload before resolving.

### `PendingEntry`

Each in-flight `mi_command_async` is tracked by a `PendingEntry`:

```python
@dataclass
class PendingEntry:
    future: asyncio.Future
    expect_socket: bool = False
    mi_response: dict | None = None
    socket_response: object | None = None
```

`_pending` maps `token → PendingEntry`.

### MI return values

Convenience functions return one of three strings:

| Value         | Future outcome                      |
|---------------|-------------------------------------|
| `"done"`      | Wait for socket data, then resolve  |
| `"failed"`    | `RuntimeError("gdb failed")`        |
| `"cancelled"` | `asyncio.CancelledError("cancelled")` |

For regular MI commands (`expect_socket=False`), the Future resolves
immediately with the MI response dict.

### Resolution flow

```
  MI arrives first          Socket arrives first
  ┌──────────────────────┐  ┌──────────────────────┐
  │ "done":              │  │ Set socket_response   │
  │   set mi_response    │  │                       │
  │   check socket_resp  │  │ If mi_response set:   │
  │     → if set: resolve│  │   resolve with data   │
  │     → else: wait     │  │ Else:                 │
  │                      │  │   wait for MI         │
  │ "failed":            │  │                       │
  │   set_exception(RE)  │  │                       │
  │                      │  │                       │
  │ "cancelled":         │  │                       │
  │   set_exception(CE)  │  │                       │
  └──────────────────────┘  └──────────────────────┘
```

### Timeout

When a timeout fires, `mi_command_async` removes the entry from
`_pending`, sets `TimeoutError` on the Future, and (for `expect_socket`
commands) sends a cancel token to GDB.  Any late-arriving MI or socket
response finds no entry in `_pending` and is silently dropped.

### Cleanup

`_fail_pending_futures` clears `_pending` and sets exceptions on all
in-flight Futures.  The `finally` block in `mi_command_async` also
removes per-token entries on timeout, cancellation, or shutdown.

## Implementation files

| File                               | Role                                           |
|------------------------------------|-------------------------------------------------|
| `tgdb/tgdb_pysetup.py`            | GDB-side: socket registration, event handlers, collection functions, convenience function classes |
| `tgdb/gdb_controller/socket_data.py` | tgdb-side: `SocketDataMixin` — frame parser, event coalescing, data dispatch and handlers |
| `tgdb/gdb_controller/controller.py`| Socket creation (`socket.socketpair()`), fd lifecycle, asyncio reader registration |
| `tgdb/gdb_controller/requests.py`  | MI request helpers that trigger socket-based collection |
