"""tgdb entry point — command-line interface."""

import argparse
import os
import sys


def _positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a positive integer, got {value!r}") from None
    if number <= 0:
        raise argparse.ArgumentTypeError(f"expected a positive integer, got {value!r}")
    return number


def _wait_for_debugger() -> None:
    from pudb.remote import set_trace

    set_trace()


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="tgdb",
        usage="tgdb [options] [gdb options] [program [core|pid]]\n       tgdb [options] [gdb options] --args program [arguments...]",
        description="tgdb — a Python front-end for GDB (cgdb reimplementation)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        # No abbreviations: an unknown long option must reach GDB intact
        # instead of being matched as a prefix of a tgdb option.
        allow_abbrev=False,
        epilog="""
Arguments that are not tgdb options (the program, a core file or PID, and
any GDB option such as -p PID or -nx) are passed to GDB in order.

Examples:
  tgdb myprogram
  tgdb -d /usr/bin/gdb myprogram
  tgdb --args myprogram arg1 arg2
  tgdb -p 12345
  tgdb myprogram core
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
        "--args",
        action="store_true",
        help="The next argument is the program; the rest are its arguments (like gdb --args)",
    )
    parser.add_argument(
        "--log",
        metavar="FILE",
        default=None,
        help="Write debug log to FILE (default: no log)",
    )
    parser.add_argument(
        "--input",
        metavar="FILE",
        default=None,
        help="Execute script FILE after startup; stop at first error, then wait for user input",
    )
    parser.add_argument(
        "--batch",
        metavar="FILE",
        default=None,
        help="Execute script FILE after startup; exit on first error or when script finishes",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run without a terminal (no UI rendering); useful for CI/batch testing",
    )
    parser.add_argument(
        "--width",
        metavar="W",
        type=_positive_int,
        default=None,
        help="Run the TUI with width W columns",
    )
    parser.add_argument(
        "--height",
        metavar="H",
        type=_positive_int,
        default=None,
        help="Run the TUI with height H rows",
    )
    parser.add_argument(
        "--cd",
        metavar="DIR",
        help="Change to DIR before starting GDB",
    )
    # The program, core/PID and GDB options are not declared: everything
    # parse_known_args() does not recognise goes to GDB in its original
    # order (cgdb-style).  ``--args`` keeps GDB's meaning: the next
    # argument is the program and the rest are the program's arguments,
    # so nothing after it is parsed as a tgdb option.
    argv = sys.argv[1:]
    program_args: list[str] = []
    if "--args" in argv:
        idx = argv.index("--args")
        argv, program_args = argv[:idx], argv[idx + 1 :]
        if not program_args:
            parser.error("--args requires a program")
    args, gdb_args = parser.parse_known_args(argv)
    if program_args:
        gdb_args.extend(["--args", *program_args])

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
        script_input=args.input,
        script_batch=args.batch,
        target_width = args.width,
        target_height = args.height,
    )
    return_code = app.run(mouse=True, headless=args.headless)
    if args.batch and app.startup_script_error:
        # The status bar is gone once the app exits; surface the reason.
        print(f"tgdb: {args.batch}: {app.startup_script_error}", file=sys.stderr)

    if log_enabled:
        from .log import shutdown as log_shutdown

        log_shutdown()

    sys.exit(return_code or 0)


if __name__ == "__main__":
    main()
