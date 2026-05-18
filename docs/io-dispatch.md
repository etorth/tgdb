# I/O Dispatch Architecture

How tgdb processes data arriving from GDB over the three I/O channels:
**console PTY**, **MI PTY**, and **AF_UNIX socket**.

---

## Overview

```
┌──────────────────────────────────────────────────────────┐
│                    asyncio event loop                     │
│                                                          │
│  loop.add_reader(fd, callback)                           │
│       │              │              │                     │
│       ▼              ▼              ▼                     │
│  _on_console_   _on_mi_        _on_sock_                 │
│   readable()    readable()      readable()               │
│       │              │              │                     │
│       ▼              ▼              ▼                     │
│  read → buf    read → buf     read → buf                 │
│       │              │              │                     │
│       ▼              ▼              ▼                     │
│  spawn_eager    spawn_eager    spawn_eager                │
│   _task()        _task()        _task()                   │
│   (per chunk)    (per record)   (per frame)              │
│       │              │              │                     │
│       ▼              ▼              ▼                     │
│  _process_     _dispatch_mi_  _process_sock_             │
│  console_data   _record()      _frame()                  │
└──────────────────────────────────────────────────────────┘
```

Each channel follows the same pattern:

1. **`add_reader` callback** — fires the instant the fd has data.
   Reads raw bytes into a per-channel buffer.  Never blocks.
2. **Extract complete units** — parse one or more complete packets
   (newline-delimited lines for MI, length-prefixed frames for socket,
   raw byte chunks for console).
3. **Spawn one eager-started task per unit** via `spawn_eager_task()`.
   Each task runs independently; no task blocks another channel.

---

## `spawn_eager_task(coro, task_set, *, name)`

```python
loop = asyncio.get_running_loop()
task = asyncio.Task(coro, loop=loop, eager_start=True, name=name)
```

- **Eager start**: the coroutine begins executing immediately inside the
  constructor, synchronously up to its first real `await`.
- If the coroutine completes without suspending, `task.done()` is `True`
  on return — zero scheduling overhead, no bookkeeping.
- If the coroutine suspends (hits an `await` that actually yields),
  the task is added to `task_set` (for GC protection) and a
  done-callback removes it automatically when it finishes.
- Unhandled exceptions are logged at `ERROR` level.

This gives the best of both worlds: synchronous operations (forwarding
console bytes, resolving result futures) run inline with no task overhead,
while truly async operations (data collection after `*stopped`) get their
own concurrent task.

---

## Console PTY (`controller.py`)

| Stage | Function | Notes |
|-------|----------|-------|
| fd readable | `_on_console_readable()` | `self._proc.read(4096)` → `_console_buf` |
| spawn | `_spawn_console_processing()` | One task per read batch |
| process | `_process_console_data(data)` | Forwards bytes to `on_console()` UI callback |

The console channel is simple: raw bytes from GDB's primary PTY are
forwarded verbatim to the GDB console widget.  No parsing needed.

**EOF handling**: `EOFError` from the PTY read signals GDB has exited.
This resolves `_console_done`, which unblocks `run_async()` and triggers
the cleanup/shutdown path.

---

## MI PTY (`controller.py`)

| Stage | Function | Notes |
|-------|----------|-------|
| fd readable | `_on_mi_readable()` | `os.read(_mi_master_fd, 4096)` → `_mi_buf` (str) |
| extract | `_spawn_mi_record_tasks()` | Split on `\n`, parse each line |
| spawn | one `spawn_eager_task()` per parsed record | — |
| process | `_dispatch_mi_record(rec)` | Routes by record type |

Record types:

- **`result`** (`token^class,...`) → `_handle_result(rec)`:
  Resolves the `asyncio.Future` in `_pending[token]`.  May trigger
  follow-up async work (e.g. frame-result data collection).

- **`notify`** (`*stopped`, `=thread-selected`, `*running`, etc.) →
  `_handle_async(rec)`:
  Drives debugger state transitions.  Can freely `await` MI round-trips
  (e.g. requesting locals after a stop) because each record runs in its
  own task — independent of the fd-readable callback.

### Why each record gets its own task

Before this design, a single serial dispatch loop processed MI records
one at a time.  This caused a **self-deadlock**: when `*stopped` handling
awaited `request_current_frame_locals()` (which sends an MI command and
awaits the `^done` response), the same dispatch loop that would process
that `^done` was blocked.

With per-record tasks: task A (`*stopped`) suspends waiting for token 7.
When `7^done` arrives, `_on_mi_readable` fires and creates task B.
Task B resolves the future.  Task A resumes.  No deadlock.

---

## AF_UNIX Socket (`socket_data.py`)

| Stage | Function | Notes |
|-------|----------|-------|
| fd readable | `_on_sock_readable()` | Drains socket → `_sock_buf` (bytes) |
| extract | `_spawn_sock_frame_tasks()` → `_extract_one_sock_frame()` | Binary protocol, one frame at a time |
| spawn | one `spawn_eager_task()` per extracted frame | — |
| process | `_process_sock_frame(frame)` | Routes by tag byte |

The socket carries structured data from GDB's embedded Python
(`tgdb_pysetup.py`).  The wire protocol is a simple TLV scheme:

| Tag | Payload | Meaning |
|-----|---------|---------|
| `O`, `F`, `C` | none | objfiles changed |
| `X` | none | GDB exiting |
| `I` | 1 byte (0=pre, 1=post) | inferior call boundary |
| `R` | varint (regnum) | register changed |
| `l`, `s`, `r`, `f`, `b` | varint(MI-token) + JSON | locals, stack, registers, frame-info, breakpoints |
| `D` | varint-len + UTF-8 | debug log from GDB-side Python |

Variable-length payloads may be zlib-compressed (indicated by a control
byte flag).

### Data-tag handling

For tags that carry JSON data (`l`, `s`, `r`, `f`, `b`):

1. The MI token prefix correlates the socket data with its originating
   MI command, enabling `_try_resolve_sock_pending()` to complete
   two-part request/response flows.
2. `_dispatch_sock_data()` routes to type-specific handlers
   (`_handle_sock_locals`, `_handle_sock_frame_info`, etc.).

The `f` (frame-info) tag is the only one that triggers follow-up async
work — it may issue additional MI commands to resolve source file paths.

---

## Task lifecycle and GC

All spawned tasks are tracked in a single `_io_tasks: set[asyncio.Task]`
on the controller.  This serves two purposes:

1. **GC protection** — Python's GC may collect a task that has no strong
   references, silently dropping the coroutine.  The set keeps suspended
   tasks alive.
2. **Cleanup on shutdown** — `run_async()`'s `finally` block cancels all
   tasks in `_io_tasks` when GDB exits.  `terminate()` does the same for
   forced shutdown.

Tasks that complete eagerly (synchronously) are never added to the set —
`task.done()` is already `True`, so no bookkeeping is needed.

---

## Cancellation

In-flight MI requests use cancel tokens.  When a new request of the same
kind arrives before the previous one completes, the old cancel token is
sent to GDB (via `-exec-interrupt` or similar), the old future is
rejected with `GDBRequestCancelled`, and the new request proceeds.

This is orthogonal to the task dispatch mechanism — cancellation operates
on the MI request/response layer, not on asyncio tasks.
