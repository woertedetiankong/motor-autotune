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
aborted                    4     ... because the device reported a fault or
                                 stopped answering
error                      2     usage or bench-file error
error                      4     the tool, the adapter or the device raised
                                 an error; the actuator could not be reached
safe_state_not_confirmed   4     the actuator may still be live
=========================  ====  ==========================================

The rule behind the exit codes: 2 when nothing was wrong with the actuator
but the request or the bench file was; 3 when the tool's own safety layer
stopped or refused motion; 4 when the device or the link to it is the
problem, or the safe state could not be confirmed. ``judge_run`` applies it to
a runner result. ``needs_human`` says, for every result, whether an agent must
stop and hand over to a human rather than try something else.
"""

from __future__ import annotations

import traceback
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any

from servobench.experiments import monitor as checks
from servobench.experiments import runner
from servobench.experiments.runner import Outcome, RunResult


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


def unexpected_error(command: str, error: BaseException) -> CommandResult:
    """The result for an exception that no command expected: a bug, reported in full."""
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
_DEVICE_KINDS = (checks.DEVICE_FAULT, checks.MISSED_REPLIES)
# Refusals at validation: the request or the bench file, before any contact.
_USAGE_KINDS = (runner.SPEC_INVALID, runner.ADAPTER_UNSUITABLE)


def judge_run(result: RunResult) -> RunJudgement | None:
    """The verdict a run's outcome forces, or None if it completed and the analysis decides."""
    stop = result.stop
    kind = None if stop is None else stop.kind
    message = "" if stop is None else stop.message

    if result.outcome is Outcome.SAFE_STATE_NOT_CONFIRMED:
        return RunJudgement(
            SAFE_STATE_NOT_CONFIRMED,
            ExitCode.DEVICE,
            True,
            "Tell the human now that the actuator may still be powered and driving its output,"
            " and that they should cut its power. Run nothing against it, read-only commands"
            " included, until a human has checked it.",
            kind,
        )
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
