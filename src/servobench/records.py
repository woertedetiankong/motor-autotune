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
``readings.csv``
    Readings a command took itself, outside any experiment, with the actuator
    left as it was (for example ``check``'s read-only items): ``t_s`` (adapter
    clock) and one column per signal.
``plot.png``
    Commanded and measured position, and temperature, against time. For a run
    refused before the actuator was enabled, the rehearsal is plotted instead
    and the title says so. If drawing fails, the rest of the record is still
    written and run.json says why in ``plot_problem``.

A file is left out when there is nothing to put in it, for example no
``samples.csv`` when the actuator was never enabled; ``run.json`` lists the
files that were written. Floats are written with all their digits, so
``load_run`` gives back exactly what was written.

``run.json``'s ``summary`` is the command's one-sentence summary (what it
printed); ``run_summary`` is the experiment runner's own. ``ended_utc`` is when
the command got the actuator back from the runner, in its safe state, from the
computer's calendar clock: the next invocation reads it to hold the cool-down
(``latest_enabled_run``). A command that only read from the actuator, and never
ran an experiment, writes its record with ``write_readonly_run``: no spec,
outcome, safe state or traces, which ``load_run`` gives back as None. A
command made of several runs (``characterize``) writes each run's record with
``write_run`` and then one record of its own with ``write_command_record``:
no spec or trace of its own, its ``parts`` (the run directories of its runs)
and the files it wrote into its directory.
"""

from __future__ import annotations

import csv
import dataclasses
import datetime
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from servobench import __version__
from servobench.clock import utc_now
from servobench.contract import Description, Fault, Sample
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
from servobench.limits import Bench
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
READINGS_FILE = "readings.csv"

# Every verdict a command may give. What each means, and the exit code that goes
# with it, is in docs/agent-usage.md and servobench.commands.
VERDICTS = (
    "pass",  # everything the command set out to establish was measured and is fine
    "partial",  # nothing failed, but some items were not measured (skipped or unknown)
    "fail",  # the measurement ran and found something wrong
    "refused",  # the tool would not enable the actuator; nothing moved
    "aborted",  # the actuator was enabled and the run was stopped before the end
    "error",  # the tool, the adapter or the device raised an error
    "safe_state_not_confirmed",  # the actuator may still be live: a human must cut its power
)
# The verdict a run gets when its command does not give one.
DEFAULT_VERDICTS = {
    Outcome.COMPLETED: "pass",
    Outcome.REFUSED: "refused",
    Outcome.ABORTED: "aborted",
    Outcome.INTERRUPTED: "aborted",
    Outcome.ERROR: "error",
    Outcome.SAFE_STATE_NOT_CONFIRMED: "safe_state_not_confirmed",
}

_SAMPLE_META = ("t_s", "elapsed_s", "segment")


class RecordError(Exception):
    """A run directory is missing, incomplete or of an unknown format."""


@dataclass(frozen=True)
class RunRecord:
    """A run directory, loaded back. A read-only record has no spec, outcome or safe state."""

    path: Path
    info: dict[str, Any]  # run.json as written
    spec: ExperimentSpec | None
    outcome: Outcome | None
    stop: StopReason | None
    safe_state: SafeStateReport | None
    verdict: str
    metrics: dict[str, Any]
    trace: Trace | None
    rehearsal: Trace | None
    readings: tuple[Sample, ...] = ()


@dataclass(frozen=True)
class RunSummary:
    """The first facts about a run directory, from its run.json alone."""

    path: Path
    name: str
    command: str
    target: str  # "<adapter>-<device id>", as in the directory name
    adapter: str
    device_id: str | None
    written_utc: datetime.datetime
    ended_utc: datetime.datetime
    verdict: str
    outcome: str | None  # the runner's outcome; None for a read-only record
    summary: str
    enabled: bool  # whether the actuator was enabled during the run


def write_run(
    result: RunResult,
    *,
    command: str,
    runs_dir: str | Path = RUNS_DIR,
    verdict: str | None = None,
    metrics: Mapping[str, Any] | None = None,
    summary: str | None = None,
    ended_utc: datetime.datetime | None = None,
    readings: Sequence[Sample] = (),
) -> Path:
    """Write ``result`` as a new run directory under ``runs_dir``; return its path.

    ``command`` is the command that ran it, for example ``"check"``.
    ``verdict`` defaults to one that follows from the outcome; a command that
    analysed the run gives its own. ``metrics`` are the command's numbers,
    each named with its unit. ``summary`` is the command's own one-sentence
    summary (default: the runner's). ``ended_utc`` is when ``run_experiment``
    returned (default: now). ``readings`` are samples the command took itself,
    outside the experiment.
    """
    verdict = DEFAULT_VERDICTS[result.outcome] if verdict is None else verdict
    _check_verdict(verdict)
    written_at = utc_now()
    identity = result.description.identity
    path = _run_directory(runs_dir, command, identity.adapter, identity.device_id, written_at)

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
    if readings:
        _write_readings(path / READINGS_FILE, readings)
        files[READINGS_FILE] = "readings taken outside the experiment"

    info = {
        "format": FORMAT,
        "tool_version": __version__,
        "command": command,
        "written_utc": written_at.isoformat(),
        "ended_utc": (ended_utc or written_at).isoformat(),
        "target": _target(identity.adapter, identity.device_id),
        "verdict": verdict,
        "outcome": result.outcome.value,
        "summary": result.summary if summary is None else summary,
        "run_summary": result.summary,
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


def write_readonly_run(
    *,
    command: str,
    bench: Bench,
    description: Description | None,
    verdict: str,
    summary: str,
    metrics: Mapping[str, Any] | None = None,
    readings: Sequence[Sample] = (),
    faults: Sequence[Fault] | None = None,
    notes: Sequence[str] = (),
    runs_dir: str | Path = RUNS_DIR,
) -> Path:
    """Write the record of a command that only read from the actuator; return its path.

    No experiment ran and the actuator's state was not changed, so there is no
    spec, outcome, safe state, trace or plot. ``description`` is None when the
    actuator could not be described (for example it did not respond).
    """
    _check_verdict(verdict)
    written_at = utc_now()
    if description is not None:
        adapter, device_id = description.identity.adapter, description.identity.device_id
    else:
        target = bench.target
        adapter, device_id = target.adapter, None if target.id is None else str(target.id)
    path = _run_directory(runs_dir, command, adapter, device_id, written_at)
    files: dict[str, str] = {}
    if readings:
        _write_readings(path / READINGS_FILE, readings)
        files[READINGS_FILE] = "readings taken by the command; the actuator was left as it was"
    info = {
        "format": FORMAT,
        "tool_version": __version__,
        "command": command,
        "written_utc": written_at.isoformat(),
        "ended_utc": written_at.isoformat(),
        "target": _target(adapter, device_id),
        "verdict": verdict,
        "outcome": None,
        "summary": summary,
        "run_summary": None,
        "stop": None,
        "safe_state": None,
        "metrics": _plain(dict(metrics or {})),
        "bench": {
            "source": bench.source,
            "fingerprint": bench.fingerprint,
            "limits": _plain(bench.limits),
            "runner": _plain(bench.runner),
        },
        "description": _plain(description),
        "spec": None,
        "rehearsal_params": None,
        "preflight": None,
        "faults_before": _plain(faults),
        "faults_after": None,
        "cooled_s": 0.0,
        "notes": list(notes),
        "trace": None,
        "rehearsal": None,
        "files": files,
        "plot_problem": None,
    }
    (path / RUN_FILE).write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")
    return path


def new_run_directory(
    command: str,
    adapter: str,
    device_id: str | None,
    runs_dir: str | Path = RUNS_DIR,
) -> Path:
    """A new, empty run directory, named as ``write_run`` names one.

    For a command that writes files of its own into its record before the
    record itself (``write_command_record``).
    """
    return _run_directory(runs_dir, command, adapter, device_id, utc_now())


def write_command_record(
    path: str | Path,
    *,
    command: str,
    bench: Bench,
    description: Description | None,
    verdict: str,
    summary: str,
    metrics: Mapping[str, Any] | None = None,
    files: Mapping[str, str] | None = None,
    parts: Sequence[str | Path] = (),
    notes: Sequence[str] = (),
) -> Path:
    """Write run.json for a command made of several runs, into ``path``; return ``path``.

    ``characterize`` runs several experiments, each with its own run record
    (``write_run``), and then fits a model to them. This record holds what
    belongs to the whole command: its verdict and metrics, the files it wrote
    into ``path`` (``files``: name in ``path`` -> what it holds), and the run
    directories of its experiments (``parts``). It has no spec, outcome, safe
    state or trace of its own: those are in the parts, which ``load_run``
    reads like any other record.
    """
    _check_verdict(verdict)
    path = Path(path)
    written_at = utc_now()
    if description is not None:
        adapter, device_id = description.identity.adapter, description.identity.device_id
    else:
        target = bench.target
        adapter, device_id = target.adapter, None if target.id is None else str(target.id)
    info = {
        "format": FORMAT,
        "tool_version": __version__,
        "command": command,
        "written_utc": written_at.isoformat(),
        "ended_utc": written_at.isoformat(),
        "target": _target(adapter, device_id),
        "verdict": verdict,
        "outcome": None,
        "summary": summary,
        "run_summary": None,
        "stop": None,
        "safe_state": None,
        "metrics": _plain(dict(metrics or {})),
        "bench": {
            "source": bench.source,
            "fingerprint": bench.fingerprint,
            "limits": _plain(bench.limits),
            "runner": _plain(bench.runner),
        },
        "description": _plain(description),
        "spec": None,
        "rehearsal_params": None,
        "preflight": None,
        "faults_before": None,
        "faults_after": None,
        "cooled_s": 0.0,
        "notes": list(notes),
        "trace": None,
        "rehearsal": None,
        "parts": [str(part) for part in parts],
        "files": dict(files or {}),
        "plot_problem": None,
    }
    (path / RUN_FILE).write_text(json.dumps(info, indent=2) + "\n", encoding="utf-8")
    return path


def list_runs(runs_dir: str | Path = RUNS_DIR) -> tuple[list[RunSummary], list[str]]:
    """Every run directory under ``runs_dir``, newest first, and a line per one that is not.

    Reads only each run.json. A missing ``runs_dir`` has no runs.
    """
    runs_dir = Path(runs_dir)
    if not runs_dir.is_dir():
        return [], []
    found: list[RunSummary] = []
    problems: list[str] = []
    for path in sorted(runs_dir.iterdir()):
        if not path.is_dir():
            continue
        try:
            found.append(_summary_of(path))
        except RecordError as error:
            problems.append(str(error))
    found.sort(key=lambda run: (run.written_utc, run.name), reverse=True)
    return found, problems


def latest_enabled_run(
    adapter: str, device_id: str | None, runs_dir: str | Path = RUNS_DIR
) -> RunSummary | None:
    """The run that most recently ended with this actuator enabled, from the records.

    "This actuator" is the same adapter name and device id. A record that
    cannot be read is passed over.
    """
    runs, _ = list_runs(runs_dir)
    matching = [
        run
        for run in runs
        if run.enabled and run.adapter == adapter and run.device_id == device_id
    ]
    return max(matching, key=lambda run: run.ended_utc, default=None)


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
    safe = info["safe_state"]
    return RunRecord(
        path=path,
        info=info,
        spec=None if info["spec"] is None else spec_from_dict(info["spec"]),
        outcome=None if info["outcome"] is None else Outcome(info["outcome"]),
        stop=_stop_from(info["stop"]),
        safe_state=None
        if safe is None
        else SafeStateReport(
            status=SafeState(safe["status"]),
            attempts=safe["attempts"],
            errors=tuple(safe["errors"]),
            t_s=safe["t_s"],
        ),
        verdict=info["verdict"],
        metrics=info["metrics"],
        trace=_trace_from(info["trace"], path, SAMPLES_FILE, COMMANDS_FILE),
        rehearsal=_trace_from(
            info["rehearsal"], path, REHEARSAL_SAMPLES_FILE, REHEARSAL_COMMANDS_FILE
        ),
        readings=_read_readings(path / READINGS_FILE)
        if READINGS_FILE in info.get("files", {})
        else (),
    )


# -- writing ----------------------------------------------------------------------------


def _check_verdict(verdict: str) -> None:
    if verdict not in VERDICTS:
        raise ValueError(f"verdict must be one of {VERDICTS}, got {verdict!r}")


def _target(adapter: str, device_id: str | None) -> dict[str, Any]:
    return {"adapter": adapter, "device_id": device_id}


def _run_directory(
    runs_dir: str | Path,
    command: str,
    adapter: str,
    device_id: str | None,
    written_at: datetime.datetime,
) -> Path:
    target = adapter + (f"-{device_id}" if device_id else "")
    name = f"{written_at:%Y%m%dT%H%M%SZ}-{_safe(command)}-{_safe(target)}"
    return _new_directory(Path(runs_dir), name)


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


def _write_readings(path: Path, readings: Sequence[Sample]) -> None:
    names: list[str] = []
    for sample in readings:
        names += [name for name in sample.values if name not in names]
    with open(path, "w", newline="", encoding="utf-8") as file:
        writer = csv.writer(file)
        writer.writerow(["t_s", *names])
        for sample in readings:
            writer.writerow([repr(sample.t_s), *(_cell(sample.values.get(name)) for name in names)])


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


def plain(value: Any) -> Any:
    """``value`` as plain JSON data, as run.json holds it: dataclasses and mappings as
    objects, enums as their values, dimensions as their names and SI units."""
    return _plain(value)


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


def _summary_of(path: Path) -> RunSummary:
    try:
        info = json.loads((path / RUN_FILE).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise RecordError(f"{path}: no {RUN_FILE}; not a run directory") from None
    except (OSError, json.JSONDecodeError) as error:
        raise RecordError(f"{path / RUN_FILE}: cannot be read: {error}") from None
    if not isinstance(info, dict) or info.get("format") != FORMAT:
        raise RecordError(f"{path}: unknown record format")
    try:
        target = info.get("target") or {
            "adapter": info["description"]["identity"]["adapter"],
            "device_id": info["description"]["identity"]["device_id"],
        }
        written = datetime.datetime.fromisoformat(info["written_utc"])
        ended = datetime.datetime.fromisoformat(info.get("ended_utc") or info["written_utc"])
        trace = info["trace"]
        adapter, device_id = target["adapter"], target["device_id"]
        return RunSummary(
            path=path,
            name=path.name,
            command=info["command"],
            target=adapter + (f"-{device_id}" if device_id else ""),
            adapter=adapter,
            device_id=device_id,
            written_utc=written,
            ended_utc=ended,
            verdict=info["verdict"],
            outcome=info["outcome"],
            summary=info["summary"],
            enabled=trace is not None and trace.get("t_enable_s") is not None,
        )
    except (KeyError, TypeError, ValueError) as error:
        problem = f"{type(error).__name__}: {error}"
        raise RecordError(f"{path / RUN_FILE}: incomplete: {problem}") from None


def _read_readings(path: Path) -> tuple[Sample, ...]:
    try:
        with open(path, newline="", encoding="utf-8") as file:
            rows = list(csv.reader(file))
    except FileNotFoundError:
        raise RecordError(f"{path}: missing") from None
    if not rows or rows[0][:1] != ["t_s"]:
        raise RecordError(f"{path}: expected the column t_s first")
    names = rows[0][1:]
    return tuple(
        Sample(
            t_s=float(row[0]),
            values={
                name: float(cell) for name, cell in zip(names, row[1:], strict=True) if cell != ""
            },
        )
        for row in rows[1:]
    )


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
