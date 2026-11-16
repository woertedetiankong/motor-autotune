"""Commands: thin layers over the library, each returning a ``CommandResult``.

A command never prints and never exits; ``servobench.cli`` renders the result
as text or JSON and exits with its code. Everything a reader needs to decide
what to do next is in the result: the verdict, one sentence, the exit code,
whether a human must now take over, and what to do.

Verdicts (``records.VERDICTS``) and exit codes:

=========================  ====  ==========================================
verdict                    exit  meaning
=========================  ====  ==========================================
pass                       0     everything was measured and is fine
partial                    0     nothing failed, but some items were not
                                 measured: they are listed as skipped or
                                 unknown, never as passed
fail                       1     the measurement ran and found a problem
                           4     ... and the problem is a fault the device
                                 itself reports (only a human can clear it)
refused                    2     the request or the bench file does not
                                 allow the experiment; nothing was contacted
refused                    3     the safety layer would not enable the
                                 actuator (limits, temperature, cool-down,
                                 rehearsal); nothing moved
refused                    4     ... because the device reports a fault or
                                 does not answer
aborted                    3     the actuator was enabled and the safety
                                 monitor stopped it, or Ctrl-C did
aborted                    4     ... because the device reported a fault,
                                 stopped answering, or answered too slowly
                                 (no good reading for sample_gap_max, or the
                                 run fell behind its plan)
error                      2     usage or bench-file error
error                      4     the tool, the adapter or the device raised
                                 an error; the actuator could not be reached;
                                 or the command's record or export could not
                                 be written
safe_state_not_confirmed   4     the actuator may still be live
=========================  ====  ==========================================

The rule behind the exit codes: 2 when nothing was wrong with the actuator
but the request or the bench file was; 3 when the tool's own safety layer
stopped or refused motion; 4 when the device or the link to it is the
problem, the tool failed, or the safe state could not be confirmed.
``judge_run`` applies it to a runner result. ``needs_human`` says, for every
result, whether an agent must stop and hand over to a human rather than try
something else.

Two rules hold for every command, and live here so that each command applies
them the same way:

- ``keep_safe_state_report``: once any run in a command has ended with its safe
  state not confirmed, the verdict is ``safe_state_not_confirmed``, whatever
  happened after (a record that cannot be written, an analysis or fit that
  fails, any exception), and nothing in the result says the actuator is safe.
  A command wraps itself in ``runner.watch_safe_state()``, which the runner
  tells before ``run_experiment`` returns, and passes every result it returns
  or records through this function.
- ``record_not_written``: a record or export that cannot be written is a
  failure of the command (``error``, exit 4), not a footnote, because the
  files are what the command was run for; the numbers measured are still
  returned. A ``safe_state_not_confirmed`` verdict stays.
"""

from __future__ import annotations

import traceback
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any

from servobench.experiments import monitor as checks
from servobench.experiments import runner
from servobench.experiments.runner import Outcome, RunResult, SafeStateWatch


class ExitCode(IntEnum):
    PASS = 0  # pass or partial
    FAIL = 1  # the measurement ran and the verdict is fail
    USAGE = 2  # usage or bench-file error; nothing was contacted
    SAFETY = 3  # the safety layer stopped or refused motion
    DEVICE = 4  # device or communication error, or the safe state not confirmed


PASS = "pass"
PARTIAL = "partial"
FAIL = "fail"
REFUSED = "refused"
ABORTED = "aborted"
ERROR = "error"
SAFE_STATE_NOT_CONFIRMED = "safe_state_not_confirmed"

# What to do when the safe state was not confirmed. Every such result's next starts with it.
SAFE_STATE_NEXT = (
    "Tell the human now that the actuator may still be powered and driving its output,"
    " and that they should cut its power. Run nothing against it, read-only commands"
    " included, until a human has checked it."
)
# How every such result's summary starts.
SAFE_STATE_SUMMARY_START = "SAFE STATE NOT CONFIRMED"


@dataclass
class CommandResult:
    """What a command found, ready to print as text or JSON."""

    command: str
    verdict: str
    summary: str  # one sentence, verdict first
    exit_code: ExitCode
    needs_human: bool  # an agent must stop and tell the human; it must not try something else
    next: str | None = None  # what to do about it, when there is something to do
    metrics: dict[str, Any] = field(default_factory=dict)
    files: dict[str, str] = field(default_factory=dict)  # path -> what it holds
    run_dir: str | None = None
    error: dict[str, str] | None = None  # an unexpected exception: type, message, traceback

    def to_json(self) -> dict[str, Any]:
        """The one JSON object ``--json`` prints, verdict first."""
        data: dict[str, Any] = {
            "verdict": self.verdict,
            "summary": self.summary,
            "exit_code": int(self.exit_code),
            "needs_human": self.needs_human,
            "next": self.next,
            "command": self.command,
            "metrics": self.metrics,
            "files": self.files,
            "run_dir": self.run_dir,
        }
        if self.error is not None:
            data["error"] = self.error
        return data


def error_details(error: BaseException) -> dict[str, str]:
    """An exception as plain data, with its traceback, so that it is reported, not hidden."""
    return {
        "type": type(error).__name__,
        "message": str(error),
        "traceback": "".join(traceback.format_exception(error)),
    }


def keep_safe_state_report(result: CommandResult, watch: SafeStateWatch | None) -> CommandResult:
    """``result``, made to say so if any run inside ``watch`` left the safe state unconfirmed.

    The verdict becomes ``safe_state_not_confirmed`` (exit 4, ``needs_human``),
    the summary starts with it, and ``next`` starts with what to do. A
    ``next`` written by code that did not know (for example one that says the
    runner left the actuator in its safe state) is replaced, not kept. Safe to
    apply more than once.
    """
    if watch is None or watch.all_confirmed:
        return result
    if result.verdict != SAFE_STATE_NOT_CONFIRMED:
        replaced = result.verdict
        result.verdict = SAFE_STATE_NOT_CONFIRMED
        tool_failed = (
            " servobench also failed after that run; give the human the traceback under error."
            if result.error is not None
            else ""
        )
        result.next = f"{SAFE_STATE_NEXT}{tool_failed}"
        if not result.summary.startswith(SAFE_STATE_SUMMARY_START):
            result.summary = (
                f"{_unconfirmed_lead(watch)} What the command reported after that (it would"
                f" have been {replaced}): {result.summary}"
            )
    elif not result.summary.startswith(SAFE_STATE_SUMMARY_START):
        result.summary = f"{_unconfirmed_lead(watch)} {result.summary}"
    if not (result.next or "").startswith(SAFE_STATE_NEXT):
        result.next = f"{SAFE_STATE_NEXT} {result.next or ''}".strip()
    result.exit_code = ExitCode.DEVICE
    result.needs_human = True
    return result


def _unconfirmed_lead(watch: SafeStateWatch) -> str:
    seen = watch.unconfirmed
    first = seen[0]
    runs = "a run" if len(seen) == 1 else f"{len(seen)} runs"
    target = first.adapter + (f" {first.device_id}" if first.device_id else "")
    return (
        f"{SAFE_STATE_SUMMARY_START}. After {runs} of this command the device ({target}) never"
        f" confirmed its safe state ({first.safe_state}) in {first.report.attempts} attempts."
        " Treat the actuator as live and still driving: cut its power now, and run nothing until"
        " a human has checked it."
    )


def record_not_written(
    result: CommandResult, error: BaseException, what: str, consequence: str
) -> CommandResult:
    """``result`` after ``what`` (the record, an export) could not be written.

    The files are the command's product, so failing to write them is a failure
    of the command: the verdict becomes ``error`` with exit 4, unless it is
    ``safe_state_not_confirmed``, which nothing replaces. ``needs_human`` is
    set, the metrics are left as they are, so the numbers measured are still
    returned, and ``consequence`` says what is missing because of it.
    """
    result.summary += f" {what} could not be written: {type(error).__name__}: {error}."
    if result.verdict != SAFE_STATE_NOT_CONFIRMED:
        result.verdict = ERROR
        result.exit_code = ExitCode.DEVICE
    result.needs_human = True
    result.next = f"{result.next or ''} {consequence}".strip()
    if result.error is None:
        result.error = error_details(error)
    return result


def unexpected_error(command: str, error: BaseException) -> CommandResult:
    """The result for an exception that no command expected: a bug, reported in full.

    Its ``next`` assumes every run's safe state was confirmed; the CLI passes
    it through ``keep_safe_state_report``, which replaces that when it was not.
    """
    return CommandResult(
        command=command,
        verdict=ERROR,
        summary=f"Error: servobench failed with an unexpected {type(error).__name__}: {error}",
        exit_code=ExitCode.DEVICE,
        needs_human=True,
        next=(
            "This is a fault in servobench, not a finding about the actuator. Tell the human and"
            " give them the traceback under error. The experiment runner puts the actuator in its"
            " safe state before it returns, whatever happened, so an error here, outside it,"
            " never leaves the actuator enabled; `servobench runs show` shows the last record."
        ),
        error=error_details(error),
    )


@dataclass(frozen=True)
class RunJudgement:
    """What a runner outcome means for a command's verdict and exit code."""

    verdict: str
    exit_code: ExitCode
    needs_human: bool
    next: str
    stop_kind: str | None


# Stop kinds that mean the device or the link to it is the problem.
_DEVICE_KINDS = (
    checks.DEVICE_FAULT,
    checks.MISSED_REPLIES,
    checks.SAMPLE_GAP,
    runner.BEHIND_SCHEDULE,
)
# Refusals at validation: the request or the bench file, before any contact.
_USAGE_KINDS = (runner.SPEC_INVALID, runner.ADAPTER_UNSUITABLE)


def judge_run(result: RunResult) -> RunJudgement | None:
    """The verdict a run's outcome forces, or None if it completed and the analysis decides."""
    stop = result.stop
    kind = None if stop is None else stop.kind
    message = "" if stop is None else stop.message

    if result.outcome is Outcome.SAFE_STATE_NOT_CONFIRMED:
        return RunJudgement(SAFE_STATE_NOT_CONFIRMED, ExitCode.DEVICE, True, SAFE_STATE_NEXT, kind)
    if result.outcome is Outcome.COMPLETED:
        return None
    if result.outcome is Outcome.INTERRUPTED:
        return RunJudgement(
            ABORTED,
            ExitCode.SAFETY,
            True,
            "The run was interrupted by Ctrl-C and the actuator is in its safe state. Run it"
            " again only if the human asks for it.",
            kind,
        )
    if result.outcome is Outcome.ERROR:
        if kind == runner.EXCEPTION:
            text = (
                f"servobench itself failed ({message}); this is a fault in the tool, not a"
                " finding about the actuator. The actuator is in its safe state. Tell the human."
            )
        else:
            text = (
                f"The adapter reported an error ({message}). The actuator is in its safe state."
                " Tell the human: check the connection, the power and the device; do not retry"
                " until the cause is known."
            )
        return RunJudgement(ERROR, ExitCode.DEVICE, True, text, kind)

    verdict = ABORTED if result.outcome is Outcome.ABORTED else REFUSED
    if kind in _DEVICE_KINDS:
        state = (
            "The run was stopped and the actuator is in its safe state."
            if verdict == ABORTED
            else "The actuator was not enabled."
        )
        if kind == checks.DEVICE_FAULT:
            text = (
                f"The device reports a fault ({message}). {state} The tool cannot clear faults:"
                " a human must find the cause and clear it (often by power-cycling the"
                " actuator). Tell the human and run nothing until they have."
            )
        elif kind == runner.BEHIND_SCHEDULE:
            text = (
                f"The run could not keep to its plan ({message}). {state} Its samples do not"
                " follow the planned experiment; do not analyse them. Tell the human: check the"
                " cable, the bus settings, and whether the computer was busy or asleep."
            )
        elif kind == checks.SAMPLE_GAP:
            text = (
                f"The device did not answer in time ({message}). {state} Tell the human:"
                " check the cable, the connector, the power and the bus settings."
            )
        else:
            text = (
                f"The device stopped answering ({message}). {state} Tell the human: check the"
                " cable, the connector, the power and the bus settings."
            )
        return RunJudgement(verdict, ExitCode.DEVICE, True, text, kind)
    if verdict == REFUSED and kind in _USAGE_KINDS:
        return RunJudgement(
            REFUSED,
            ExitCode.USAGE,
            True,
            f"Nothing was contacted: {message}. The experiment does not fit the bench limits or"
            " the adapter. The bench file belongs to the owner: tell them what was refused, and"
            " do not change a limit to get past it.",
            kind,
        )
    if verdict == REFUSED and kind == runner.COOLDOWN:
        return RunJudgement(
            REFUSED,
            ExitCode.SAFETY,
            False,
            f"{message}. Nothing moved. Wait that long and run the command again; this is the"
            " cool-down the bench file asks for, not a fault.",
            kind,
        )
    if verdict == REFUSED:
        return RunJudgement(
            REFUSED,
            ExitCode.SAFETY,
            True,
            f"The actuator was not enabled: {message}. Tell the human what was refused; do not"
            " change the bench file or the experiment to get past a limit.",
            kind,
        )
    return RunJudgement(
        ABORTED,
        ExitCode.SAFETY,
        True,
        f"The safety monitor stopped the run: {message}. The actuator is in its safe state."
        " Stop, tell the human what happened, and run nothing else until they have looked at it.",
        kind,
    )
