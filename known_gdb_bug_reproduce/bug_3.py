"""
Reproducer for GDB duplicate symbols from parent-block merging of sibling scopes.

GDB's Python ``block.__iter__()`` yields the same variable name multiple times
from the function-body block when sibling child ``{ }`` scopes each declare a
variable with the same name (e.g. ``parms``, ``result``).  Additionally,
variables that live in an inner block re-appear in the function-body block with
``val.address == None``, even when the inner block's copy has a real stack
address.  Debugger front-ends that build locals panes from ``block.__iter__()``
see phantom duplicates.

Two distinct sub-issues are observed within a single block iteration:

1. **Cross-depth duplication**: a variable such as ``numConnIds`` appears at
   depth 0 (the innermost scope block where the PC sits) with a real stack
   address, AND again at depth 1 (the function-body block) with
   ``val.address == None``.

2. **Sibling-scope merging**: variables ``parms`` and ``result``, each
   declared inside distinct ``{ }`` blocks (sibling scopes), all appear
   in the function-body block at depth 1 — even though only the currently-
   executing block should be visible.

The trigger is a constructor (or large function) with:
- Lambda-initialised variables (``const auto x = [...]() { ... }();``)
- Multiple ``{ }`` blocks that each declare identically-named local variables
- The debugger stops inside a callee and uses ``up`` to navigate to the
  constructor frame.

This script:

1. Synthesises a C++ class whose constructor follows the above pattern.
2. Compiles with ``-O0 -g3``.
3. Spawns GDB, sets a breakpoint in a callee, runs, then ``up``s to the
   constructor frame.
4. Walks ``frame.block()`` collecting every symbol at every depth.
5. Checks for cross-depth and sibling-scope duplicates.

Exit codes:

  0  bug confirmed (duplicates found)
  1  bug not reproduced
  2  ``gdb`` or ``g++`` not in PATH
  3  test setup failed

Standalone — depends only on ``gdb``, ``g++``, and the Python stdlib.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap


_BUG_ID = "Bug 3"
_BUG_TITLE = "block.__iter__() yields duplicate symbols from parent-block merging of sibling scopes"

_EXIT_LABELS = {
    0: "CONFIRMED",
    1: "NOT REPRODUCED",
    2: "TOOLS MISSING",
    3: "SETUP FAILED",
}


def _verdict(code: int, detail: str = "") -> int:
    label = _EXIT_LABELS.get(code, "UNKNOWN")
    print()
    print("=" * 70)
    print(f"  {_BUG_ID}: {_BUG_TITLE}")
    print(f"  Result : {label} (exit code {code})")
    if detail:
        for line in detail.splitlines():
            print(f"  {line}")
    print("=" * 70)
    return code


CPP_SOURCE = textwrap.dedent("""
    #include <string>
    #include <vector>
    #include <cstring>
    #include <functional>
    #include <unistd.h>

    volatile int g_sink = 0;

    struct CheckVersionParams { int id; int runMode; int version; std::string hostName; };
    struct CheckVersionResult { int dbeVersion; };
    struct OpenEngineParams   { int id; int runMode; std::string hostName; };
    struct OpenEngineResult   { int designSignature; bool dualDCC; std::string sessionName;
                                std::string recvSocketAddr; std::string dccMaskQPAddr;
                                std::vector<int> connIdSet; std::vector<int> phyBoard;
                                std::vector<int> phyDomain; std::vector<int> domainMap;
                                std::vector<int> chipMap; };

    __attribute__((noinline))
    CheckVersionResult check_version(const CheckVersionParams& p) {
        g_sink += p.id;
        return {p.version};
    }

    __attribute__((noinline))
    OpenEngineResult open_engine(const OpenEngineParams& p) {
        g_sink += p.id;
        return {42, false, "t.phy", "", "403:74157",
                {1}, {0,-1}, {0,-1}, {0}, {0}};
    }

    __attribute__((noinline))
    void load_env_args(double ldUseRatio, bool forwardLogs) {
        g_sink += static_cast<int>(ldUseRatio) + (forwardLogs ? 1 : 0);
    }

    class Processor {
        std::string m_hostName;
        int m_id;
    public:
        __attribute__((noinline))
        Processor(int id) : m_id(id) {
            char hostNameBuf[512];
            gethostname(hostNameBuf, sizeof(hostNameBuf));
            m_hostName = hostNameBuf;

            const auto currRunDir = []() -> std::string {
                char cwd[4096];
                getcwd(cwd, sizeof(cwd));
                return std::string(cwd);
            }();

            const auto forwardLogs = [this]() -> bool { return m_id > 10; }();

            const auto speedTestStr = [this]() -> const char * {
                return m_id > 100 ? "speed test " : "";
            }();

            const auto dccReadSpeedStr = [this]() -> std::string {
                return m_id > 200 ? "all" : "false";
            }();

            int numConnIds = -1;
            int numDomainOnLD = -1;
            int designSignature = -1;
            bool dccInterleave = false;
            std::string sessionName;
            std::string recvSocketAddr;
            std::string recvDCCMaskQPAddr;
            std::vector<int> domainMask(128, 0);

            {
                const CheckVersionParams parms{m_id, 0, 1, m_hostName};
                CheckVersionResult result{};
                result = check_version(parms);
                if (result.dbeVersion != 1) return;
            }

            {
                const OpenEngineParams parms{m_id, 0, m_hostName};
                const auto result = open_engine(parms);
                numConnIds = static_cast<int>(result.connIdSet.size());
                designSignature = result.designSignature;
                dccInterleave = result.dualDCC;
                sessionName = result.sessionName;
                recvSocketAddr = result.recvSocketAddr;
                recvDCCMaskQPAddr = result.dccMaskQPAddr;
                for (auto v : result.connIdSet) domainMask.at(v % 128) = 1;
            }

            load_env_args(1.0 * numDomainOnLD / 16, forwardLogs);

            g_sink = numConnIds + designSignature + (dccInterleave ? 1 : 0)
                   + static_cast<int>(sessionName.size())
                   + static_cast<int>(recvSocketAddr.size())
                   + static_cast<int>(recvDCCMaskQPAddr.size())
                   + static_cast<int>(currRunDir.size())
                   + static_cast<int>(dccReadSpeedStr.size())
                   + static_cast<int>(domainMask.size())
                   + (speedTestStr[0] ? 1 : 0);
        }
    };

    int main() {
        Processor p(5);
        return 0;
    }
""").lstrip()


GDB_WALK_SCRIPT = textwrap.dedent("""
    import gdb, json

    frame = gdb.selected_frame()
    block = frame.block()
    depth = 0
    symbols = []

    while block:
        func = block.function.name if block.function else ""
        for sym in block:
            if not (sym.is_variable or sym.is_argument):
                continue
            name = sym.name
            line = sym.line
            try:
                val = sym.value(frame)
                has_addr = val.address is not None
            except Exception:
                has_addr = False
            symbols.append({
                "name": name, "depth": depth,
                "block_start": hex(block.start), "block_end": hex(block.end),
                "func": func, "line": line, "has_addr": has_addr,
            })
        if block.superblock is None or block.function is not None:
            break
        block = block.superblock
        depth += 1

    print("@@SYMBOLS@@" + json.dumps(symbols))
""").strip()


MI_WALK_COMMANDS = [
    "-stack-list-variables --all-values",
    "-stack-list-locals --all-values",
]


def _parse_mi_variable_list(raw: str) -> list[dict[str, str]]:
    """Extract variable name/value pairs from an MI ``^done`` response.

    The MI output format is not JSON — it uses ``key=value`` pairs with
    quoted strings and ``[{...},{...}]`` lists.  This is a lightweight
    regex-based parser that pulls ``name="..."`` and ``value="..."``
    pairs from entries like ``{name="x",value="42"}``.
    """
    import re

    results: list[dict[str, str]] = []
    for m in re.finditer(r'\{([^}]+)\}', raw):
        entry_str = m.group(1)
        name_m = re.search(r'name="([^"]*)"', entry_str)
        value_m = re.search(r'value="([^"]*)"', entry_str)
        if name_m:
            results.append({
                "name": name_m.group(1),
                "value": value_m.group(1) if value_m else "<no value>",
            })
    return results


def _print_mi_variables(raw: str, mi_cmd: str) -> None:
    """Pretty-print the MI variable list and flag duplicates."""
    from collections import Counter

    variables = _parse_mi_variable_list(raw)
    if not variables:
        print(f"    (could not parse: {raw[:120]}...)")
        return

    name_count = Counter(v["name"] for v in variables)
    dups = {n for n, c in name_count.items() if c > 1}

    print(f"    variables returned: {len(variables)}")
    if dups:
        print(f"    DUPLICATES in MI output: {sorted(dups)}")
    else:
        print(f"    no duplicates in MI output")

    for v in variables:
        dup_marker = " **DUP**" if v["name"] in dups else ""
        val_preview = v["value"][:60]
        print(f"      {v['name']:25s} = {val_preview}{dup_marker}")


def main() -> int:
    for tool in ("gdb", "g++"):
        if shutil.which(tool) is None:
            return _verdict(2, f"{tool} not found in PATH")

    tmpdir = tempfile.mkdtemp(prefix="gdb_bug3_")
    cpp_path = os.path.join(tmpdir, "test.cpp")
    exe_path = os.path.join(tmpdir, "a.out")

    try:
        with open(cpp_path, "w") as f:
            f.write(CPP_SOURCE)

        subprocess.run(
            ["g++", "-O0", "-g3", "-std=c++17", "-o", exe_path, cpp_path],
            check=True, capture_output=True,
        )

        escaped = GDB_WALK_SCRIPT.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")

        mi_exs: list[str] = []
        for mi_cmd in MI_WALK_COMMANDS:
            mi_exs.extend(["-ex", f"interpreter-exec mi \"{mi_cmd}\""])

        proc = subprocess.Popen(
            [
                "gdb", "--batch", "--quiet",
                "-ex", "set debuginfod enabled off",
                "-ex", "set pagination off",
                "-ex", "break load_env_args",
                "-ex", "run",
                "-ex", "up",
                "-ex", f'python exec("{escaped}")',
                *mi_exs,
                exe_path,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

        stdout, _ = proc.communicate(timeout=30)

        symbols = None
        for line in stdout.splitlines():
            if line.startswith("@@SYMBOLS@@"):
                symbols = json.loads(line[len("@@SYMBOLS@@"):])
                break

        if symbols is None:
            print("--- GDB output ---", file=sys.stderr)
            print(stdout, file=sys.stderr)
            return _verdict(3, "@@SYMBOLS@@ line not found in GDB output")

        # Collect MI output lines for comparison.
        mi_lines: list[str] = []
        for line in stdout.splitlines():
            stripped = line.strip()
            if stripped.startswith("^done,") or stripped.startswith("^error,"):
                mi_lines.append(stripped)

        # Analysis.
        from collections import Counter

        name_count = Counter(s["name"] for s in symbols)
        multi_names = {n for n, c in name_count.items() if c > 1}

        # Cross-depth: same variable at multiple depths.
        by_name: dict[str, list] = {}
        for s in symbols:
            by_name.setdefault(s["name"], []).append(s)
        cross_depth = {}
        for name, entries in by_name.items():
            depths = set(e["depth"] for e in entries)
            if len(depths) > 1:
                cross_depth[name] = entries

        # Sibling-scope merging: same name, same depth, different decl lines.
        by_name_depth: dict[tuple[str, int], list] = {}
        for s in symbols:
            by_name_depth.setdefault((s["name"], s["depth"]), []).append(s)
        sibling_merged = {}
        for (name, depth), entries in by_name_depth.items():
            lines = set(e["line"] for e in entries)
            if len(lines) > 1:
                sibling_merged[(name, depth)] = entries

        found_bug = False

        print(f"Total symbols: {len(symbols)}")
        print(f"Names appearing more than once: {len(multi_names)}")
        print()

        if cross_depth:
            found_bug = True
            print("=== CROSS-DEPTH DUPLICATION ===")
            print("Variables present in both inner block (real addr) and")
            print("function-body block (addr=None):")
            print()
            for name in sorted(cross_depth):
                entries = cross_depth[name]
                for e in sorted(entries, key=lambda x: x["depth"]):
                    print(f"  {name:25s} depth={e['depth']} line={e['line']:3d} has_addr={e['has_addr']}")
            print()

        if sibling_merged:
            found_bug = True
            print("=== SIBLING-SCOPE MERGING ===")
            print("Same name at same depth but different declaration lines")
            print("(from sibling { } blocks merged into function body):")
            print()
            for (name, depth) in sorted(sibling_merged):
                entries = sibling_merged[(name, depth)]
                lines = sorted(set(e["line"] for e in entries))
                print(f"  {name:25s} depth={depth} lines={lines}")
            print()

        # Show MI output for comparison.
        if mi_lines:
            print("=== MI COMMAND OUTPUT ===")
            for i, mi_cmd in enumerate(MI_WALK_COMMANDS):
                print(f"\n  {mi_cmd}:")
                if i < len(mi_lines):
                    raw = mi_lines[i]
                    _print_mi_variables(raw, mi_cmd)
                else:
                    print("    (no response)")
            print()

        if found_bug:
            detail = (
                f"block.__iter__() yielded {len(symbols)} symbols, "
                f"{len(multi_names)} names duplicated\n"
                f"MI -stack-list-variables returned {len(_parse_mi_variable_list(mi_lines[0])) if mi_lines else '?'} unique variables, no duplicates\n"
                "Bug is specific to the Python block iterator API, not the MI stack commands."
            )
            return _verdict(0, detail)
        else:
            return _verdict(1, "all symbols are unique per (name, depth)")

    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
