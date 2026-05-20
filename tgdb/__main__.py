"""tgdb entry point — command-line interface."""

import argparse
import os
import sys


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
  tgdb --pid 12345
""",
    )
    parser.add_argument(
        "-d",
        "--debugger",
        metavar="DEBUGGER",
        default="gdb",
        help="Path to GDB executable (default: gdb)",
    )
    parser.add_argument(
        "-w",
        "--wait",
        action="store_true",
        help="Wait for debugger before continuing startup",
    )
    parser.add_argument(
        "-r",
        "--rcfile",
        metavar="FILE",
        default=None,
        help="Read configuration from FILE instead of ~/.config/tgdb/tgdbrc; use NONE to skip",
    )
    parser.add_argument(
        "-p",
        "--pid",
        metavar="PID",
        type=int,
        default=None,
        help="Attach to running process with given PID",
    )
    parser.add_argument(
        "--args",
        action="store_true",
        help="Pass remaining arguments as program + arguments to GDB",
    )
    parser.add_argument(
        "--log",
        metavar="FILE",
        default=None,
        help="Write debug log to FILE (default: no log)",
    )
    parser.add_argument(
        "--cd",
        metavar="DIR",
        help="Change to DIR before starting GDB",
    )
    parser.add_argument(
        "program",
        nargs="?",
        help="Program to debug",
    )
    parser.add_argument(
        "core_or_pid",
        nargs="?",
        help="Core file or PID to attach",
    )

    # Support --args: everything after --args is program + its args
    if "--args" in sys.argv:
        idx = sys.argv.index("--args")
        pre_args = sys.argv[1:idx]
        post_args = sys.argv[idx + 1 :]
        args = parser.parse_args(pre_args)
        gdb_args = ["--args"] + post_args
    else:
        args = parser.parse_args()
        gdb_args = []
        if args.pid is not None:
            gdb_args.extend(["-p", str(args.pid)])
        else:
            if args.program:
                gdb_args.append(args.program)
            if args.core_or_pid:
                gdb_args.append(args.core_or_pid)

    if args.cd:
        os.chdir(args.cd)
    if args.wait:
        _wait_for_debugger()

    # Initialise logging before importing anything heavy.
    log_enabled = bool(args.log)
    if log_enabled:
        from .log import init as log_init

        log_init(args.log)

    # Import here to avoid loading textual before arg parsing
    from .app import TGDBApp

    app = TGDBApp(
        gdb_path=args.debugger,
        gdb_args=gdb_args,
        rc_file=args.rcfile,
    )
    app.run(mouse=True)

    if log_enabled:
        from .log import shutdown as log_shutdown

        log_shutdown()


if __name__ == "__main__":
    main()
