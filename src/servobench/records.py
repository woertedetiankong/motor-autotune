"""Run records: one directory of plain files per run, under ``runs/``.

A run directory is named ``<UTC date and time>-<command>-<target>``, for example
``runs/20261116T142233Z-check-sim-1``, and holds:

``run.json``
    The run description, as indented JSON: format version, tool version,
    command, when it was written (UTC), the bench file's path and fingerprint,
    the limits and runner settings in force, the adapter's description, the
    experiment spec, the model parameters used for the rehearsal, the outcome
    and its one-line summary, the reason the run stopped (if it did), the
    safe-state report, the verdict and metrics, the faults before and after,
    the preflight reading, the runner's notes, and for the real run and the
    rehearsal: the segments with their start times, the enable and start and
    end times, the missed reads, and how the run stopped. Every value is SI
    under a name that ends in its unit.
``samples.csv``
    The real run's samples, one row each: ``t_s`` (adapter clock),
    ``elapsed_s`` (since the experiment started; empty before), ``segment``
    (index into run.json's segments; empty before the first), and one column
    per signal, named with its unit (``position_rad``, ``temperature_degc``...).
    A safety abort keeps every sample up to the one that stopped the run.
``commands.csv``
    The real run's commands: ``t_s`` (when the adapter sent it), ``elapsed_s``,
    ``segment``, and one column per command name (``position_rad``).
``rehearsal_samples.csv``, ``rehearsal_commands.csv``
    The same for the rehearsal on the simulated servo, on its own clock.
``plot.png``
    Commanded and measured position, and temperature, against time. For a run
    refused before the actuator was enabled, the rehearsal is plotted instead
    and the title says so. If drawing fails, the rest of the record is still
    written and run.json says why in ``plot_problem``.

A file is left out when there is nothing to put in it, for example no
``samples.csv`` when the actuator was never enabled; ``run.json`` lists the
files that were written. Floats are written with all their digits, so
``load_run`` gives back exactly what was written.
"""

from __future__ import annotations

import csv
import dataclasses
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from servobench import __version__
from servobench.clock import utc_now
from servobench.contract import Sample
from servobench.experiments.runner import (
    CommandRecord,
    MissedRead,
    Outcome,
    RunResult,
    SafeState,
    SafeStateReport,
    StopReason,
    Trace,
)
from servobench.experiments.spec import ExperimentSpec, Segment, spec_from_dict
from servobench.plots import plot_run
from servobench.units import Dimension

FORMAT = "servobench-run/1"
RUNS_DIR = "runs"
RUN_FILE = "run.json"
SAMPLES_FILE = "samples.csv"
COMMANDS_FILE = "commands.csv"
REHEARSAL_SAMPLES_FILE = "rehearsal_samples.csv"
REHEARSAL_COMMANDS_FILE = "rehearsal_commands.csv"
PLOT_FILE = "plot.png"

# The verdict a run gets when its command does not give one (see the plan's output format).
DEFAULT_VERDICTS = {
    Outcome.COMPLETED: "pass",
    Outcome.REFUSED: "aborted",
    Outcome.ABORTED: "aborted",
    Outcome.INTERRUPTED: "aborted",
    Outcome.ERROR: "error",
    Outcome.SAFE_STATE_NOT_CONFIRMED: "error",
}
VERDICTS = ("pass", "fail", "aborted", "error")

_SAMPLE_META = ("t_s", "elapsed_s", "segment")


class RecordError(Exception):
    """A run directory is missing, incomplete or of an unknown format."""


@dataclass(frozen=True)
class RunRecord:
    """A run directory, loaded back."""

    path: Path
    info: dict[str, Any]  # run.json as written
    spec: ExperimentSpec
    outcome: Outcome
    stop: StopReason | None
    safe_state: SafeStateReport
    verdict: str
    metrics: dict[str, Any]
    trace: Trace | None
    rehearsal: Trace | None


def write_run(
    result: RunResult,
    *,
    command: str,
    runs_dir: str | Path = RUNS_DIR,
    verdict: str | None = None,
    metrics: Mapping[str, Any] | None = None,
) -> Path:
    """Write ``result`` as a new run directory under ``runs_dir``; return its path.

    ``command`` is the command that ran it, for example ``"check"``.
    ``verdict`` defaults to one that follows from the outcome; a command that
    analysed the run gives its own. ``metrics`` are the command's numbers,
    each named with its unit.
    """
    verdict = DEFAULT_VERDICTS[result.outcome] if verdict is None else verdict
    if verdict not in VERDICTS:
        raise ValueError(f"verdict must be one of {VERDICTS}, got {verdict!r}")
    written_at = utc_now()
    identity = result.description.identity
    target = identity.adapter + (f"-{identity.device_id}" if identity.device_id else "")
    name = f"{written_at:%Y%m%dT%H%M%SZ}-{_safe(command)}-{_safe(target)}"
    path = _new_directory(Path(runs_dir), name)

    files: dict[str, str] = {}
    for trace, samples_file, commands_file in (
        (result.trace, SAMPLES_FILE, COMMANDS_FILE),
        (result.rehearsal, REHEARSAL_SAMPLES_FILE, REHEARSAL_COMMANDS_FILE),
    ):
        if trace is None:
            continue
        if trace.samples:
            _write_samples(path / samples_file, trace)
            files[samples_file] = "samples" if trace is result.trace else "rehearsal samples"
        if trace.commands:
            _write_commands(path / commands_file, trace)
            files[commands_file] = "commands" if trace is result.trace else "rehearsal commands"

    # The plot last: if drawing fails, the record keeps everything else.
    plot_problem = None
    try:
        plotted = _plot(result, path / PLOT_FILE, command)
    except Exception as error:  # noqa: BLE001 - reported in run.json
        plotted, plot_problem = None, f"{type(error).__name__}: {error}"
    if plotted:
        files[PLOT_FILE] = plotted

    info = {
        "format": FORMAT,
        "tool_version": __version__,
        "command": command,
        "written_utc": written_at.isoformat(),
        "verdict": verdict,
        "outcome": result.outcome.value,
        "summary": result.summary,
        "stop": _plain(result.stop),
        "safe_state": _plain(result.safe_state),
        "metrics": _plain(dict(metrics or {})),
        "bench": {
            "source": result.bench_source,
            "fingerprint": result.bench_fingerprint,
            "limits": _plain(result.limits),
            "runner": _plain(result.runner),
        },
        "description": _plain(result.description),
        "spec": result.spec.to_dict(),
        "rehearsal_params": _plain(result.rehearsal_params),
        "preflight": _plain_sample(result.preflight),
        "faults_before": _plain(result.faults_before),
        "faults_after": _plain(result.faults_after),
        "cooled_s": result.cooled_s,
        "notes": list(result.notes),
        "trace": _plain_trace(result.trace),
        "rehearsal": _plain_trace(result.rehearsal),
        "files": files,
        "plot_problem": plot_problem,
    }
    (path / RUN_FILE).write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")
    return path


def load_run(path: str | Path) -> RunRecord:
    """Load a run directory written by ``write_run``."""
    path = Path(path)
    try:
        info = json.loads((path / RUN_FILE).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise RecordError(f"{path}: no {RUN_FILE}; this is not a run directory") from None
    except json.JSONDecodeError as error:
        raise RecordError(f"{path / RUN_FILE}: not valid JSON: {error}") from None
    if info.get("format") != FORMAT:
        raise RecordError(f"{path}: unknown record format {info.get('format')!r}")
    return RunRecord(
        path=path,
        info=info,
        spec=spec_from_dict(info["spec"]),
        outcome=Outcome(info["outcome"]),
        stop=_stop_from(info["stop"]),
        safe_state=SafeStateReport(
            status=SafeState(info["safe_state"]["status"]),
            attempts=info["safe_state"]["attempts"],
            errors=tuple(info["safe_state"]["errors"]),
            t_s=info["safe_state"]["t_s"],
        ),
        verdict=info["verdict"],
        metrics=info["metrics"],
        trace=_trace_from(info["trace"], path, SAMPLES_FILE, COMMANDS_FILE),
        rehearsal=_trace_from(
            info["rehearsal"], path, REHEARSAL_SAMPLES_FILE, REHEARSAL_COMMANDS_FILE
        ),
    )


# -- writing ----------------------------------------------------------------------------


def _new_directory(runs_dir: Path, name: str) -> Path:
    runs_dir.mkdir(parents=True, exist_ok=True)
    for number in range(1, 1000):
        path = runs_dir / (name if number == 1 else f"{name}-{number}")
        try:
            path.mkdir()
        except FileExistsError:
            continue
        return path
    raise RecordError(f"{runs_dir}: too many runs named {name}")


def _write_samples(path: Path, trace: Trace) -> None:
    names: list[str] = []
    for sample in trace.samples:
        names += [name for name in sample.values if name not in names]
    with open(path, "w", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        writer.writerow([*_SAMPLE_META, *names])
        for sample in trace.samples:
            writer.writerow(
                [
                    *_meta(trace, sample.t_s),
                    *(_cell(sample.values.get(name)) for name in names),
                ]
            )


def _write_commands(path: Path, trace: Trace) -> None:
    names: list[str] = []
    for command in trace.commands:
        if command.name not in names:
            names.append(command.name)
    with open(path, "w", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        writer.writerow([*_SAMPLE_META, *names])
        for command in trace.commands:
            elapsed = trace.elapsed_s(command.t_s)
            writer.writerow(
                [
                    repr(command.t_s),
                    _cell(elapsed),
                    command.segment,
                    *(repr(command.value) if name == command.name else "" for name in names),
                ]
            )


def _meta(trace: Trace, t_s: float) -> list[str]:
    segment = trace.segment_at(t_s)
    return [repr(t_s), _cell(trace.elapsed_s(t_s)), "" if segment is None else str(segment)]


def _cell(value: float | None) -> str:
    return "" if value is None else repr(float(value))


def _plot(result: RunResult, path: Path, command: str) -> str | None:
    identity = result.description.identity
    target = identity.adapter + (f" {identity.device_id}" if identity.device_id else "")
    heading = f"{command}: {result.spec.kind} on {target}"
    if result.trace is not None and result.trace.samples:
        title = f"{heading}, {result.outcome.value.replace('_', ' ')}"
        plot_run(result.trace, result.limits, path, title=title)
        return "plot of the run"
    if result.rehearsal is not None and result.rehearsal.samples:
        title = f"{heading}: rehearsal on the simulated servo; the real actuator was not enabled"
        plot_run(
            result.rehearsal,
            result.limits,
            path,
            title=title,
            measured_label="simulated servo (rehearsal)",
        )
        return "plot of the rehearsal (the real actuator was not enabled)"
    return None


def _plain(value: Any) -> Any:
    """Plain JSON data: dataclasses and mappings as objects, enums as their values."""
    if isinstance(value, Dimension):
        return f"{value.name.lower()} ({value.si_unit})"
    if isinstance(value, Enum):
        return value.value
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {item.name: _plain(getattr(value, item.name)) for item in dataclasses.fields(value)}
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value


def _plain_sample(sample: Sample | None) -> dict[str, Any] | None:
    return None if sample is None else {"t_s": sample.t_s, "values": dict(sample.values)}


def _plain_trace(trace: Trace | None) -> dict[str, Any] | None:
    if trace is None:
        return None
    return {
        "simulated": trace.simulated,
        "segments": [segment.to_dict() for segment in trace.segments],
        "segment_start_s": list(trace.segment_start_s),
        "enable_position_rad": trace.enable_position_rad,
        "t_enable_s": trace.t_enable_s,
        "t_start_s": trace.t_start_s,
        "t_end_s": trace.t_end_s,
        "stop": _plain(trace.stop),
        "missed": _plain(trace.missed),
        "sample_count": len(trace.samples),
        "command_count": len(trace.commands),
    }


def _safe(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("_") or "unnamed"


# -- loading ----------------------------------------------------------------------------


def _stop_from(data: dict[str, Any] | None) -> StopReason | None:
    if data is None:
        return None
    values = dict(data)
    values["rehearsal"] = _stop_from(values.get("rehearsal"))
    return StopReason(**values)


def _trace_from(
    data: dict[str, Any] | None, path: Path, samples_file: str, commands_file: str
) -> Trace | None:
    if data is None:
        return None
    samples = _read_samples(path / samples_file) if data["sample_count"] else ()
    commands = _read_commands(path / commands_file) if data["command_count"] else ()
    if len(samples) != data["sample_count"] or len(commands) != data["command_count"]:
        raise RecordError(f"{path}: {samples_file} or {commands_file} does not match {RUN_FILE}")
    return Trace(
        simulated=data["simulated"],
        segments=tuple(Segment.from_dict(segment) for segment in data["segments"]),
        segment_start_s=tuple(data["segment_start_s"]),
        samples=samples,
        commands=commands,
        missed=tuple(MissedRead(**missed) for missed in data["missed"]),
        enable_position_rad=data["enable_position_rad"],
        t_enable_s=data["t_enable_s"],
        t_start_s=data["t_start_s"],
        t_end_s=data["t_end_s"],
        stop=_stop_from(data["stop"]),
    )


def _read_rows(path: Path) -> tuple[list[str], list[list[str]]]:
    try:
        with open(path, newline="", encoding="utf-8") as file:
            rows = list(csv.reader(file))
    except FileNotFoundError:
        raise RecordError(f"{path}: missing") from None
    if not rows or list(rows[0][: len(_SAMPLE_META)]) != list(_SAMPLE_META):
        raise RecordError(f"{path}: expected the columns {', '.join(_SAMPLE_META)} first")
    return rows[0], rows[1:]


def _read_samples(path: Path) -> tuple[Sample, ...]:
    header, rows = _read_rows(path)
    names = header[len(_SAMPLE_META) :]
    samples = []
    for row in rows:
        values = {
            name: float(cell)
            for name, cell in zip(names, row[len(_SAMPLE_META) :], strict=True)
            if cell != ""
        }
        samples.append(Sample(t_s=float(row[0]), values=values))
    return tuple(samples)


def _read_commands(path: Path) -> tuple[CommandRecord, ...]:
    header, rows = _read_rows(path)
    names = header[len(_SAMPLE_META) :]
    commands = []
    for row in rows:
        for name, cell in zip(names, row[len(_SAMPLE_META) :], strict=True):
            if cell != "":
                commands.append(CommandRecord(float(row[0]), name, float(cell), int(row[2])))
    return tuple(commands)
