"""The runs command: list recent run records, or show one. It only reads files.

Its verdict says whether reading the records worked. The verdict of a run
being shown is that run's own, under ``metrics.run.verdict``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from servobench import records
from servobench.commands import ERROR, PASS, CommandResult, ExitCode

COMMAND = "runs"
LATEST = "latest"


def list_recent(runs_dir: str | Path = records.RUNS_DIR, limit: int = 10) -> CommandResult:
    runs, problems = records.list_runs(runs_dir)
    shown = runs[:limit]
    rows = [
        {
            "run_dir": str(run.path),
            "written_utc": run.written_utc.isoformat(),
            "command": run.command,
            "target": run.target,
            "verdict": run.verdict,
            "outcome": run.outcome,
            "summary": run.summary,
        }
        for run in shown
    ]
    if not runs:
        summary = f"No run records under {runs_dir} yet."
    else:
        newest = runs[0]
        summary = (
            f"{len(runs)} run records under {runs_dir}; showing the newest {len(shown)}. The"
            f" newest, {newest.name}, has verdict {newest.verdict}."
        )
    return CommandResult(
        command=COMMAND,
        verdict=PASS,
        summary=summary,
        exit_code=ExitCode.PASS,
        needs_human=False,
        metrics={
            "runs_dir": str(runs_dir),
            "total": len(runs),
            "runs": rows,
            "unreadable": problems,
        },
    )


def show(run: str = LATEST, runs_dir: str | Path = records.RUNS_DIR) -> CommandResult:
    """Show one run: ``run`` is a run directory's path, its name under ``runs_dir``, or latest."""
    if run == LATEST:
        runs, _ = records.list_runs(runs_dir)
        if not runs:
            return _usage(f"there are no run records under {runs_dir} to show")
        path = runs[0].path
    else:
        path = Path(run)
        if not (path / records.RUN_FILE).is_file():
            path = Path(runs_dir) / run
    try:
        info = json.loads((path / records.RUN_FILE).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _usage(f"{run} is not a run directory, nor the name of one under {runs_dir}")
    except (OSError, json.JSONDecodeError) as error:
        return _usage(f"{path / records.RUN_FILE} cannot be read: {error}")
    if not isinstance(info, dict) or info.get("format") != records.FORMAT:
        return _usage(f"{path} is not a run record this version of servobench can read")
    shown: dict[str, Any] = {
        "run_dir": str(path),
        "command": info.get("command"),
        "written_utc": info.get("written_utc"),
        "target": info.get("target"),
        "verdict": info.get("verdict"),
        "outcome": info.get("outcome"),
        "summary": info.get("summary"),
        "run_summary": info.get("run_summary"),
        "stop": info.get("stop"),
        "safe_state": info.get("safe_state"),
        "metrics": info.get("metrics"),
        "notes": info.get("notes"),
        "bench_fingerprint": (info.get("bench") or {}).get("fingerprint"),
    }
    files = {str(path / records.RUN_FILE): "the run record (run.json)"}
    files.update({str(path / name): what for name, what in (info.get("files") or {}).items()})
    return CommandResult(
        command=COMMAND,
        verdict=PASS,
        summary=f"Run {path.name} ({info.get('command')}): verdict {info.get('verdict')}."
        f" {info.get('summary')}",
        exit_code=ExitCode.PASS,
        needs_human=False,
        metrics={"run": shown},
        files=files,
        run_dir=str(path),
    )


def _usage(problem: str) -> CommandResult:
    return CommandResult(
        command=COMMAND,
        verdict=ERROR,
        summary=f"Error: {problem}.",
        exit_code=ExitCode.USAGE,
        needs_human=False,
        next="Run `servobench runs` to list the run records, then show one by its name.",
    )
