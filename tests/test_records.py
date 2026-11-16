"""Run records: plain files that a person can read and the tool can load back."""

from __future__ import annotations

import csv
import datetime
import json
import math
import subprocess
import sys

import matplotlib.image
import pytest

from servobench import __version__, records
from servobench.adapters.sim import SimConfig, SimServo
from servobench.contract import TEMPERATURE, NoReply, dimension_of_name
from servobench.experiments.runner import Outcome, SafeState, run_experiment
from servobench.experiments.spec import Hold, Step
from servobench.limits import TEMPLATE, parse_bench
from servobench.model import PendulumLoad
from servobench.records import RecordError, load_run, write_run

DEG = math.radians(1.0)
LOAD = PendulumLoad(mass_kg=0.05, com_distance_m=0.1)
BENCH = parse_bench(TEMPLATE.encode())
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def servo(*faults, **config):
    return SimServo(SimConfig(faults=frozenset(faults), **config), load=LOAD)


@pytest.fixture(scope="module")
def completed():
    return run_experiment(servo(), BENCH, Step(0.0, (10 * DEG, -10 * DEG), 1.0, 0.5))


@pytest.fixture(scope="module")
def aborted():
    return run_experiment(servo("overheating"), BENCH, Hold(0.0, 25.0))


def assert_complete_record(path, result, command):
    """Everything plan step 5 lists is in the record, and it loads back exactly."""
    info = json.loads((path / records.RUN_FILE).read_text(encoding="utf-8"))
    assert info["format"] == records.FORMAT
    assert info["tool_version"] == __version__
    assert info["command"] == command
    assert info["bench"]["fingerprint"] == BENCH.fingerprint
    assert info["bench"]["limits"]["temperature_max_degc"] == 60.0
    assert info["bench"]["runner"]["missed_replies_max"] == 3
    assert info["description"]["identity"]["adapter"] == "sim"
    assert info["description"]["signals"][0]["dimension"] == "angle (rad)"
    assert info["description"]["capabilities"]["temperature_cutoff"] == "yes"
    assert info["spec"] == result.spec.to_dict()
    assert info["outcome"] == result.outcome.value
    assert info["summary"] == result.summary
    assert info["safe_state"]["status"] == "confirmed"
    assert "verdict" in info and "metrics" in info

    record = load_run(path)
    assert record.spec == result.spec
    assert record.outcome is result.outcome
    assert record.stop == result.stop
    assert record.safe_state == result.safe_state
    assert record.trace == result.trace  # samples, commands, segments: exactly
    assert record.rehearsal == result.rehearsal

    # Units are in every column name of the CSV files.
    for name in (records.SAMPLES_FILE, records.COMMANDS_FILE):
        with open(path / name, newline="", encoding="utf-8") as file:
            header = next(csv.reader(file))
        assert header[:3] == ["t_s", "elapsed_s", "segment"]
        for column in header[3:]:
            assert dimension_of_name(column) is not None, column

    # The plot is a valid image.
    plot = path / records.PLOT_FILE
    assert plot.read_bytes()[:8] == PNG_SIGNATURE
    height, width, _ = matplotlib.image.imread(plot).shape
    assert height > 400 and width > 600
    return info, record


# == The "Done when" checks of plan step 5 =======================================


def test_a_completed_run_leaves_a_complete_record_that_loads_back(completed, tmp_path):
    assert completed.outcome is Outcome.COMPLETED
    path = write_run(completed, command="check", runs_dir=tmp_path / "runs")
    assert path.parent == tmp_path / "runs"
    assert path.name.endswith("-check-sim-1")
    info, record = assert_complete_record(path, completed, "check")
    assert info["verdict"] == "pass" and record.verdict == "pass"
    assert info["stop"] is None
    assert sorted(info["files"]) == sorted(
        [
            records.SAMPLES_FILE,
            records.COMMANDS_FILE,
            records.REHEARSAL_SAMPLES_FILE,
            records.REHEARSAL_COMMANDS_FILE,
            records.PLOT_FILE,
        ]
    )


def test_an_aborted_run_keeps_its_samples_up_to_the_abort_and_the_reason(aborted, tmp_path):
    assert aborted.outcome is Outcome.ABORTED
    path = write_run(aborted, command="characterize", runs_dir=tmp_path)
    info, record = assert_complete_record(path, aborted, "characterize")
    assert info["verdict"] == "aborted"
    # The reason, in plain words and as data.
    assert info["stop"]["kind"] == "temperature"
    assert "temperature_max 60 degC" in info["stop"]["message"]
    assert info["trace"]["stop"]["kind"] == "temperature"
    assert record.stop.kind == "temperature" and record.stop.limit == 60.0
    assert "Safety abort" in info["summary"]
    # The samples up to the abort, the last one being the sample that stopped it.
    samples = record.trace.samples
    assert len(samples) == len(aborted.trace.samples) > 2000
    assert samples[-1].t_s == record.stop.t_s
    assert samples[-1][TEMPERATURE] > 60.0
    assert all(sample[TEMPERATURE] <= 60.0 for sample in samples[:-1])
    with open(path / records.SAMPLES_FILE, newline="", encoding="utf-8") as file:
        rows = list(csv.reader(file))
    assert len(rows) == 1 + len(samples)
    assert float(rows[-1][rows[0].index("temperature_degc")]) == samples[-1][TEMPERATURE]


# == Other runs and details =======================================================


def test_a_run_refused_in_rehearsal_records_the_rehearsal_and_plots_it(tmp_path):
    result = run_experiment(servo(), BENCH, Step(0.0, (30 * DEG,), 1.0, 0.5))
    assert result.outcome is Outcome.REFUSED and result.trace is None
    path = write_run(result, command="check", runs_dir=tmp_path)
    info = json.loads((path / records.RUN_FILE).read_text(encoding="utf-8"))
    assert info["stop"]["kind"] == "rehearsal_failed"
    assert info["stop"]["rehearsal"]["kind"] == "speed"
    assert info["trace"] is None
    assert not (path / records.SAMPLES_FILE).exists()
    assert "not enabled" in info["files"][records.PLOT_FILE]
    assert (path / records.PLOT_FILE).read_bytes()[:8] == PNG_SIGNATURE
    record = load_run(path)
    assert record.trace is None
    assert record.stop.rehearsal.kind == "speed"
    assert record.rehearsal == result.rehearsal


def test_a_run_whose_safe_state_failed_says_so_in_its_record(tmp_path):
    class Deaf(SimServo):
        def enter_safe_state(self):
            raise NoReply("no answer")

    result = run_experiment(Deaf(load=LOAD), BENCH, Hold(0.0, 1.0))
    path = write_run(result, command="check", runs_dir=tmp_path)
    info = json.loads((path / records.RUN_FILE).read_text(encoding="utf-8"))
    assert info["outcome"] == "safe_state_not_confirmed"
    # Its own verdict, so that it cannot be read as an ordinary error.
    assert info["verdict"] == "safe_state_not_confirmed"
    assert info["summary"].startswith("SAFE STATE NOT CONFIRMED")
    assert info["safe_state"]["status"] == "not_confirmed"
    assert load_run(path).safe_state.status is SafeState.NOT_CONFIRMED


def test_a_refusal_before_contact_still_leaves_a_record(tmp_path):
    result = run_experiment(servo(), BENCH, Hold(85 * DEG, 1.0))
    path = write_run(result, command="check", runs_dir=tmp_path)
    info = json.loads((path / records.RUN_FILE).read_text(encoding="utf-8"))
    assert info["stop"]["kind"] == "spec_invalid"
    assert info["files"] == {}
    assert load_run(path).safe_state.status is SafeState.NOT_CONTACTED


def test_verdict_and_metrics_from_the_command_are_kept(completed, tmp_path):
    metrics = {"overshoot_percent": 3.2, "rise_time_s": 0.081}
    path = write_run(completed, command="tune", runs_dir=tmp_path, verdict="fail", metrics=metrics)
    record = load_run(path)
    assert record.verdict == "fail" and record.metrics == metrics
    with pytest.raises(ValueError, match="verdict"):
        write_run(completed, command="tune", runs_dir=tmp_path, verdict="ok")


def test_run_directories_are_named_by_time_command_and_target_and_never_reused(
    completed, tmp_path, monkeypatch
):
    moment = datetime.datetime(2026, 11, 16, 14, 22, 33, tzinfo=datetime.UTC)
    monkeypatch.setattr(records, "utc_now", lambda: moment)
    first = write_run(completed, command="check", runs_dir=tmp_path)
    second = write_run(completed, command="check", runs_dir=tmp_path)
    odd = write_run(completed, command="my check/../x", runs_dir=tmp_path)
    assert first.name == "20261116T142233Z-check-sim-1"
    assert second.name == "20261116T142233Z-check-sim-1-2"
    assert odd.parent == tmp_path and "/" not in odd.name
    info = json.loads((first / records.RUN_FILE).read_text(encoding="utf-8"))
    assert info["written_utc"] == "2026-11-16T14:22:33+00:00"


def test_loading_something_that_is_not_a_record_fails_clearly(completed, tmp_path):
    with pytest.raises(RecordError, match="not a run directory"):
        load_run(tmp_path)
    path = write_run(completed, command="check", runs_dir=tmp_path)
    (path / records.SAMPLES_FILE).write_text("t_s,elapsed_s,segment,position_rad\n")
    with pytest.raises(RecordError, match="does not match"):
        load_run(path)
    (path / records.RUN_FILE).write_text('{"format": "other/9"}')
    with pytest.raises(RecordError, match="unknown record format"):
        load_run(path)


def test_a_plot_that_fails_does_not_lose_the_record(completed, tmp_path, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("no fonts")

    monkeypatch.setattr(records, "plot_run", broken)
    path = write_run(completed, command="check", runs_dir=tmp_path)
    info = json.loads((path / records.RUN_FILE).read_text(encoding="utf-8"))
    assert info["plot_problem"] == "RuntimeError: no fonts"
    assert records.PLOT_FILE not in info["files"]
    assert load_run(path).trace == completed.trace


def test_plotting_is_headless_and_never_imports_pyplot():
    code = (
        "import sys, servobench.records, servobench.plots;"
        " assert 'matplotlib.pyplot' not in sys.modules"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


# == Added for the command line (plan step 7) =======================================


def test_the_command_summary_end_time_and_readings_are_kept(completed, tmp_path):
    ended = datetime.datetime(2026, 11, 16, 9, 0, 0, tzinfo=datetime.UTC)
    readings = list(completed.trace.samples[:3])
    path = write_run(
        completed,
        command="check",
        runs_dir=tmp_path,
        summary="Pass: all items passed.",
        ended_utc=ended,
        readings=readings,
    )
    info = json.loads((path / records.RUN_FILE).read_text(encoding="utf-8"))
    assert info["summary"] == "Pass: all items passed."
    assert info["run_summary"] == completed.summary
    assert info["ended_utc"] == "2026-11-16T09:00:00+00:00"
    assert info["target"] == {"adapter": "sim", "device_id": "1"}
    assert records.READINGS_FILE in info["files"]
    assert load_run(path).readings == tuple(readings)


def test_a_read_only_record_has_no_run_and_loads_back(tmp_path):
    adapter = servo()
    readings = [adapter.read() for _ in range(5)]
    path = records.write_readonly_run(
        command="check",
        bench=BENCH,
        description=adapter.describe(),
        verdict="partial",
        summary="Partial: read only.",
        metrics={"items": {"zero": {"status": "pass"}}},
        readings=readings,
        faults=(),
        notes=("a note",),
        runs_dir=tmp_path,
    )
    assert path.name.endswith("-check-sim-1")
    record = load_run(path)
    assert record.verdict == "partial" and record.metrics["items"]["zero"]["status"] == "pass"
    assert record.spec is None and record.outcome is None and record.safe_state is None
    assert record.trace is None and record.rehearsal is None
    assert record.readings == tuple(readings)
    assert record.info["bench"]["fingerprint"] == BENCH.fingerprint
    assert sorted(record.info["files"]) == [records.READINGS_FILE]
    # Without a description (the actuator did not respond), the bench file names the target.
    unreached = records.write_readonly_run(
        command="check",
        bench=BENCH,
        description=None,
        verdict="error",
        summary="Error: no reply.",
        runs_dir=tmp_path,
    )
    assert unreached.name.endswith("-check-sim") and load_run(unreached).info["files"] == {}


def test_runs_are_listed_newest_first_and_a_broken_one_is_reported(
    completed, tmp_path, monkeypatch
):
    times = iter(
        datetime.datetime(2026, 11, 16, 9, minute, tzinfo=datetime.UTC) for minute in range(10)
    )
    monkeypatch.setattr(records, "utc_now", lambda: next(times))
    older = write_run(completed, command="check", runs_dir=tmp_path)
    newer = records.write_readonly_run(
        command="check", bench=BENCH, description=None, verdict="error", summary="x",
        runs_dir=tmp_path,
    )  # fmt: skip
    (tmp_path / "not-a-run").mkdir()
    (tmp_path / "a-file.txt").write_text("")
    runs, problems = records.list_runs(tmp_path)
    assert [run.path for run in runs] == [newer, older]
    assert runs[1].enabled and not runs[0].enabled
    assert runs[1].target == "sim-1" and runs[1].outcome == "completed"
    assert len(problems) == 1 and "not-a-run" in problems[0]
    assert records.list_runs(tmp_path / "missing") == ([], [])


def test_the_latest_enabled_run_of_one_actuator_is_found(completed, tmp_path, monkeypatch):
    refused = run_experiment(servo(), BENCH, Hold(85 * DEG, 1.0))  # never enabled
    assert refused.trace is None
    early = datetime.datetime(2026, 11, 16, 9, 0, tzinfo=datetime.UTC)
    late = datetime.datetime(2026, 11, 16, 9, 5, tzinfo=datetime.UTC)
    enabled = write_run(completed, command="check", runs_dir=tmp_path, ended_utc=early)
    write_run(refused, command="check", runs_dir=tmp_path, ended_utc=late)
    found = records.latest_enabled_run("sim", "1", tmp_path)
    assert found.path == enabled and found.ended_utc == early
    assert records.latest_enabled_run("sim", "2", tmp_path) is None  # another actuator
    assert records.latest_enabled_run("hd1910", "1", tmp_path) is None
    assert records.latest_enabled_run("sim", "1", tmp_path / "none") is None


# == Added in plan step 8 =========================================================


def test_a_command_record_holds_its_own_files_and_its_parts(completed, tmp_path):
    part = write_run(completed, command="characterize", runs_dir=tmp_path)
    path = records.new_run_directory("characterize", "sim", "1", tmp_path)
    (path / "servo_model.json").write_text("{}\n", encoding="utf-8")
    records.write_command_record(
        path,
        command="characterize",
        bench=BENCH,
        description=completed.description,
        verdict="partial",
        summary="Partial: a test.",
        metrics={"parameters": {"kp_n_m_rad": {"value": 1.8}}},
        files={"servo_model.json": "the export"},
        parts=[part],
    )
    record = load_run(path)
    assert record.verdict == "partial" and record.spec is None and record.trace is None
    assert record.info["parts"] == [str(part)]
    assert record.info["files"] == {"servo_model.json": "the export"}
    runs, problems = records.list_runs(tmp_path)
    assert problems == [] and {run.path for run in runs} == {part, path}
    # The cool-down reads the enabled part, not the command record.
    assert records.latest_enabled_run("sim", "1", tmp_path).path == part
