"""tgdb entry point — command-line interface compatible with cgdb."""
from __future__ import annotations

import argparse
import os
import sys
import time


def _wait_for_debugger() -> None:
    from pudb.remote import set_trace
    set_trace()

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="tgdb",
        description="tgdb — a Python front-end for GDB (cgdb reimplementation)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  tgdb myprogram
  tgdb -d /usr/bin/gdb myprogram
  tgdb --args myprogram arg1 arg2
  tgdb myprogram core
""",
    )
    parser.add_argument(
        "-d", "--debugger", metavar="DEBUGGER",
        default="gdb",
        help="Path to GDB executable (default: gdb)",
    )
    parser.add_argument(
        "-w", "--wait", action="store_true",
        help="Wait for debugger before continuing startup",
    )
    parser.add_argument(
        "-r", "--rcfile", metavar="FILE",
        default=None,
        help="Read configuration from FILE instead of ~/.cgdb/cgdbrc",
    )
    parser.add_argument(
        "--args", action="store_true",
        help="Pass remaining arguments as program + arguments to GDB",
    )
    parser.add_argument(
        "--cd", metavar="DIR",
        help="Change to DIR before starting GDB",
    )
    parser.add_argument(
        "program", nargs="?",
        help="Program to debug",
    )
    parser.add_argument(
        "core_or_pid", nargs="?",
        help="Core file or PID to attach",
    )

    # Support --args: everything after --args is program + its args
    if "--args" in sys.argv:
        idx = sys.argv.index("--args")
        pre_args = sys.argv[1:idx]
        post_args = sys.argv[idx + 1:]
        args = parser.parse_args(pre_args)
        gdb_args = ["--args"] + post_args
    else:
        args = parser.parse_args()
        gdb_args = []
        if args.program:
            gdb_args.append(args.program)
        if args.core_or_pid:
            gdb_args.append(args.core_or_pid)

    if args.cd:
        os.chdir(args.cd)
    if args.wait:
        _wait_for_debugger()

    # Import here to avoid loading textual before arg parsing
    from .app import TGDBApp

    app = TGDBApp(
        gdb_path=args.debugger,
        gdb_args=gdb_args,
        rc_file=args.rcfile,
    )
    app.run(mouse=True)


if __name__ == "__main__":
    main()
