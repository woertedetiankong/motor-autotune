"""Command line: a thin layer over the library."""

from __future__ import annotations

import argparse
import sys
from enum import IntEnum

from servobench import __version__
from servobench.limits import BENCH_FILENAME, write_template


class ExitCode(IntEnum):
    PASS = 0
    FAIL = 1  # the measurement ran and the verdict is fail
    USAGE = 2  # usage or bench-file error
    SAFETY_ABORT = 3
    DEVICE = 4  # device or communication error


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run safe, bounded experiments on one actuator.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", metavar="COMMAND")

    init = commands.add_parser(
        "init",
        help=f"write a commented {BENCH_FILENAME} template in this directory",
        description=f"Write a commented {BENCH_FILENAME} template in the current directory."
        " An existing file is never overwritten.",
    )
    init.set_defaults(handler=_init)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help(sys.stderr)
        return ExitCode.USAGE
    return args.handler(args)


def _init(args: argparse.Namespace) -> int:
    try:
        write_template(BENCH_FILENAME)
    except FileExistsError:
        print(
            f"error: {BENCH_FILENAME} already exists here and was left unchanged.\n"
            f"next: edit it by hand, or move it aside and run init again.",
            file=sys.stderr,
        )
        return ExitCode.USAGE
    print(
        f"Wrote {BENCH_FILENAME}. It targets the simulated servo.\n"
        f"next: before connecting real hardware, set every limit in it from the"
        f" servo's datasheet and your bench rig."
    )
    return ExitCode.PASS
