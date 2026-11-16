"""Command line: a thin layer over the library.

Each command returns a ``servobench.commands.CommandResult``; this module only
parses arguments, renders the result as text or as one JSON object
(``--json``), and exits with the result's code. The verdict always comes first.

Nothing escapes as a bare traceback: a usage error, a bench-file error, Ctrl-C
and an unexpected exception each become a result with a verdict and a non-zero
exit code, and an unexpected exception keeps its traceback, in the JSON under
``error`` and in the text output.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from typing import NoReturn

from servobench import __version__
from servobench.adapters import UnknownAdapter
from servobench.commands import (
    ERROR,
    PASS,
    SAFE_STATE_NOT_CONFIRMED,
    CommandResult,
    ExitCode,
    keep_safe_state_report,
    unexpected_error,
)
from servobench.commands import runs as runs_command
from servobench.commands.characterize import run_characterize
from servobench.commands.check import run_check
from servobench.experiments.runner import watch_safe_state
from servobench.limits import (
    BENCH_FILENAME,
    CHECK_FIELDS,
    LIMIT_FIELDS,
    BenchFileError,
    load_bench,
    write_template,
)
from servobench.units import Dimension, format_quantity

__all__ = ["ExitCode", "build_parser", "main"]


class _UsageError(Exception):
    pass


class _Parser(argparse.ArgumentParser):
    """Raises instead of exiting on a usage error, so the error can be reported as JSON."""

    def error(self, message: str) -> NoReturn:
        raise _UsageError(f"{self.prog}: {message}")


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="servobench",
        description="Run safe, bounded experiments on one actuator.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    _json_option(parser, default=False)
    commands = parser.add_subparsers(dest="command", metavar="COMMAND", parser_class=_Parser)

    init = commands.add_parser(
        "init",
        help=f"write a commented {BENCH_FILENAME} template in this directory",
        description=f"Write a commented {BENCH_FILENAME} template in the current directory."
        " An existing file is never overwritten.",
    )
    _json_option(init)
    init.set_defaults(handler=_init)

    check = commands.add_parser(
        "check",
        help="is this actuator alive and set up correctly?",
        description="Check the actuator that the bench file describes, item by item: it"
        " responds, its identity, faults, voltage, temperature, the bus, its protections, its"
        " zero, and, unless --no-motion, a small move in the positive direction and how much it"
        " warms. Every check writes a run record under runs/.",
    )
    _json_option(check)
    check.add_argument(
        "--no-motion",
        action="store_true",
        help="only read from the actuator; the items that move it are reported as skipped",
    )
    check.add_argument(
        "--bench",
        default=BENCH_FILENAME,
        metavar="PATH",
        help=f"the bench file (default: {BENCH_FILENAME} in this directory)",
    )
    check.set_defaults(handler=_check)

    characterize = commands.add_parser(
        "characterize",
        help="how does this actuator actually behave? fit the servo model and export it",
        description="Run check, then a set of steps and a stepped sine on the actuator that the"
        " bench file describes, each through the safety layer, and fit the servo model's"
        " parameters to them. Each parameter is reported with its uncertainty, or as not"
        " identifiable with the reason; the fitted model is compared with the default on a"
        " held-out experiment against the readings' noise floor; the result is exported as JSON"
        " and as a MuJoCo fragment. Every experiment writes a run record under runs/.",
    )
    _json_option(characterize)
    characterize.add_argument(
        "--bench",
        default=BENCH_FILENAME,
        metavar="PATH",
        help=f"the bench file (default: {BENCH_FILENAME} in this directory)",
    )
    characterize.set_defaults(handler=_characterize)

    runs = commands.add_parser(
        "runs",
        help="list recent run records, or show one",
        description="List the run records under runs/, newest first, or show one.",
    )
    _json_option(runs)
    runs.add_argument(
        "--limit",
        type=_count,
        default=10,
        metavar="N",
        help="how many runs to list (default: 10)",
    )
    runs.set_defaults(handler=_runs_list)
    runs_commands = runs.add_subparsers(dest="runs_command", metavar="show", parser_class=_Parser)
    show = runs_commands.add_parser(
        "show",
        help="show one run record",
        description="Show one run record: its verdict, summary, metrics and files.",
    )
    _json_option(show)
    show.add_argument(
        "run",
        nargs="?",
        default=runs_command.LATEST,
        help="a run directory, or its name under runs/ (default: the latest)",
    )
    show.set_defaults(handler=_runs_show)
    return parser


def _json_option(parser: argparse.ArgumentParser, default: object = argparse.SUPPRESS) -> None:
    # Only the top level sets the default, so --json works before or after a command name.
    parser.add_argument(
        "--json",
        action="store_true",
        default=default,
        help="print one JSON object instead of text",
    )


def _count(text: str) -> int:
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a whole number, got {text!r}") from None
    if value < 1:
        raise argparse.ArgumentTypeError(f"expected 1 or more, got {value}")
    return value


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else list(argv)
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except _UsageError as error:
        result = _usage_error(str(error), parser.format_usage().strip())
        return _emit(result, "--json" in argv)
    if args.command is None:
        result = _usage_error("no command given", parser.format_usage().strip())
        return _emit(result, args.json)
    # Whatever ends the command, a run whose safe state was not confirmed is
    # reported as such: the texts below assume every safe state was confirmed.
    with watch_safe_state() as watch:
        try:
            result = args.handler(args)
        except KeyboardInterrupt:
            result = CommandResult(
                command=args.command,
                verdict="aborted",
                summary="Interrupted by Ctrl-C, outside an experiment.",
                exit_code=ExitCode.SAFETY,
                needs_human=True,
                next=(
                    "servobench had not left the actuator enabled: it was only reading, or the"
                    " experiment runner had already put it in its safe state. Run the command"
                    " again only if the human asks for it."
                ),
            )
        except Exception as error:  # noqa: BLE001 - reported in full, never a bare traceback
            result = unexpected_error(args.command, error)
        result = keep_safe_state_report(result, watch)
    return _emit(result, args.json)


# -- the commands ------------------------------------------------------------------------


def _init(args: argparse.Namespace) -> CommandResult:
    try:
        write_template(BENCH_FILENAME)
    except FileExistsError:
        return CommandResult(
            command="init",
            verdict=ERROR,
            summary=f"Error: {BENCH_FILENAME} already exists here and was left unchanged.",
            exit_code=ExitCode.USAGE,
            needs_human=False,
            next="Edit it by hand, or move it aside and run init again.",
        )
    return CommandResult(
        command="init",
        verdict=PASS,
        summary=f"Wrote {BENCH_FILENAME}. It targets the simulated servo.",
        exit_code=ExitCode.PASS,
        needs_human=False,
        next="Before connecting real hardware, set every limit in it from the servo's datasheet"
        " and your bench rig.",
        files={BENCH_FILENAME: "the bench file template"},
    )


def _check(args: argparse.Namespace) -> CommandResult:
    try:
        bench = load_bench(args.bench)
    except BenchFileError as error:
        return _bench_error("check", error)
    try:
        return run_check(bench, motion=not args.no_motion)
    except UnknownAdapter as error:
        return _unknown_adapter("check", bench, error)


def _unknown_adapter(command: str, bench, error: UnknownAdapter) -> CommandResult:
    return CommandResult(
        command=command,
        verdict=ERROR,
        summary=f"Error: {bench.source}: {error}.",
        exit_code=ExitCode.USAGE,
        needs_human=True,
        next=f"Nothing was contacted. Tell the owner: {bench.source} names an adapter that"
        " this version of servobench does not have. The bench file is theirs to change.",
        metrics={"problems": [f"{bench.source}: {error}"]},
    )


def _characterize(args: argparse.Namespace) -> CommandResult:
    try:
        bench = load_bench(args.bench)
    except BenchFileError as error:
        return _bench_error("characterize", error)
    try:
        return run_characterize(bench)
    except UnknownAdapter as error:
        return _unknown_adapter("characterize", bench, error)


def _runs_list(args: argparse.Namespace) -> CommandResult:
    return runs_command.list_recent(limit=args.limit)


def _runs_show(args: argparse.Namespace) -> CommandResult:
    return runs_command.show(args.run)


def _bench_error(command: str, error: BenchFileError) -> CommandResult:
    problems = [f"{error.source}: {problem}" for problem in error.problems]
    first = problems[0] if problems else f"{error.source}: invalid"
    more = f" (and {len(problems) - 1} more problems)" if len(problems) > 1 else ""
    return CommandResult(
        command=command,
        verdict=ERROR,
        summary=f"Error: the bench file is not usable: {first}{more}.",
        exit_code=ExitCode.USAGE,
        needs_human=True,
        next=(
            "Nothing was contacted. The bench file belongs to its owner: tell them each problem"
            " listed under metrics.problems. Do not write limit values into it yourself; a"
            " commented template, with placeholders that suit only the simulated servo, comes"
            " from `servobench init` in an empty directory."
        ),
        metrics={"problems": problems},
    )


def _usage_error(problem: str, usage: str) -> CommandResult:
    return CommandResult(
        command="servobench",
        verdict=ERROR,
        summary=f"Error: {problem}.",
        exit_code=ExitCode.USAGE,
        needs_human=False,
        next=f"Nothing was contacted. Correct the command; {usage}. `servobench COMMAND --help`"
        " describes each command.",
    )


# -- output ------------------------------------------------------------------------------


def _emit(result: CommandResult, as_json: bool) -> int:
    try:
        if as_json:
            text = json.dumps(result.to_json(), indent=2, allow_nan=False)
        else:
            text = _text(result)
    except Exception as error:  # noqa: BLE001 - a result that cannot be printed is a bug
        failure = unexpected_error(result.command, error)
        failure.summary += f" (while printing a result whose verdict was {result.verdict})"
        if result.verdict == SAFE_STATE_NOT_CONFIRMED:
            # Nothing replaces this verdict, a failure to print it included.
            failure.verdict = SAFE_STATE_NOT_CONFIRMED
            failure.summary = (
                f"{result.summary} (servobench then failed to print the full result:"
                f" {type(error).__name__}: {error})"
            )
            failure.next = result.next
        text = json.dumps(failure.to_json(), indent=2) if as_json else _text(failure)
        result = failure
    stream = sys.stderr if result.exit_code == ExitCode.USAGE and not as_json else sys.stdout
    print(text, file=stream)
    if as_json and result.verdict == SAFE_STATE_NOT_CONFIRMED:
        # A person watching the terminal sees this even when only an agent reads the JSON.
        print(f"servobench: {result.summary}", file=sys.stderr)
    return int(result.exit_code)


def _text(result: CommandResult) -> str:
    if result.verdict == SAFE_STATE_NOT_CONFIRMED:
        head = "verdict: SAFE STATE NOT CONFIRMED (exit 4): the actuator may still be live"
    else:
        head = f"verdict: {result.verdict} (exit {int(result.exit_code)})"
    if result.needs_human:
        head += "; stop and tell the human"
    lines = [head, result.summary]
    items = result.metrics.get("items") if result.command == "check" else None
    if items:
        lines.append("")
        width = max(len(name) for name in items)
        for name, item in items.items():
            lines.append(f"  {item['status']:<8} {name:<{width}}  {item['detail']}")
    if result.command == "characterize" and "parameters" in result.metrics:
        lines += ["", *_parameters_text(result.metrics)]
    if result.next:
        lines += ["", f"next: {result.next}"]
    if result.command == "check" and "limits" in result.metrics:
        lines += ["", *_limits_text(result.metrics)]
    problems = result.metrics.get("problems")
    if problems:
        lines += ["", *(f"  {problem}" for problem in problems)]
    if result.command == "runs":
        lines += _runs_text(result.metrics)
    if result.run_dir:
        lines += ["", f"record: {result.run_dir}"]
    if result.error is not None:
        lines += ["", result.error["traceback"].rstrip()]
    return "\n".join(lines)


# How to show a quantity of each dimension to a person.
_DISPLAY_UNITS = {
    Dimension.ANGLE: "deg",
    Dimension.ANGULAR_VELOCITY: "deg/s",
    Dimension.TIME: "s",
}


def _limits_text(metrics: dict) -> list[str]:
    bench = metrics["bench"]
    lines = [f"limits in force ({bench['source']}, {bench['fingerprint'][:19]}...):"]
    for fields, values, section in (
        (LIMIT_FIELDS, metrics["limits"], "limits"),
        (CHECK_FIELDS, metrics["check_settings"], "check"),
    ):
        shown = []
        for key, attribute, dimension in fields:
            unit = _DISPLAY_UNITS.get(dimension)
            shown.append(f"{key} {format_quantity(values[attribute], dimension, unit, digits=4)}")
        lines.append(f"  [{section}] " + ", ".join(shown))
    return lines


def _parameters_text(metrics: dict) -> list[str]:
    lines = []
    for name, entry in metrics["parameters"].items():
        if entry["status"] == "identifiable":
            shown = f"{entry['value']:.4g} +/- {entry['uncertainty']:.2g} {entry['unit']}"
        elif entry.get("lower_bound") is not None:
            shown = f"not identifiable; at least {entry['lower_bound']:.3g} {entry['unit']}"
        elif entry.get("determined_only_as"):
            shown = f"not identifiable; only {entry['determined_only_as']} is determined"
        else:
            shown = "not identifiable"
        lines.append(f"  {name:<26} {shown}")
    validation = metrics["validation"]
    deg = math.degrees(1.0)
    lines.append(
        f"  held-out RMS error: fitted {validation['fitted_model']['rms_error_rad'] * deg:.4f}"
        f" deg, default {validation['default_model']['rms_error_rad'] * deg:.4f} deg, noise"
        f" floor {validation['noise_floor_rad'] * deg:.4f} deg: fit {validation['fit_quality']}"
    )
    return lines


def _runs_text(metrics: dict) -> list[str]:
    lines: list[str] = []
    for run in metrics.get("runs", []):
        lines.append(
            f"  {run['verdict']:<8} {run['command']:<6} {run['run_dir']}  {run['summary']}"
        )
    shown = metrics.get("run")
    if shown:
        items = (shown.get("metrics") or {}).get("items") or {}
        for name, item in items.items():
            lines.append(f"  {item['status']:<8} {name}  {item['detail']}")
    if lines:
        lines.insert(0, "")
    return lines
