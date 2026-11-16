"""The characterize command on the simulated servo: plan step 8.

Each full characterize takes several seconds of real time (the fit), so the
runs are made once per module and shared by the tests that read them.
"""

from __future__ import annotations

import contextlib
import io
import json
import math
import os
import re
import xml.etree.ElementTree as ElementTree
from pathlib import Path

import pytest

from servobench import records
from servobench.adapters.sim import SimConfig, SimServo
from servobench.analysis.fit import DAMPING_TOTAL, PARAMETERS
from servobench.cli import main
from servobench.commands import ExitCode
from servobench.commands.characterize import (
    GOOD_FIT_NOISE_RATIO,
    PlanError,
    plan_experiments,
    run_characterize,
)
from servobench.experiments.spec import Step, SteppedSine
from servobench.export import JSON_FILE, MUJOCO_FILE
from servobench.limits import TEMPLATE, parse_bench
from servobench.model import DEFAULT_PARAMS, PendulumLoad, ServoParams

DEG = math.radians(1.0)
LOAD = PendulumLoad(mass_kg=0.05, com_distance_m=0.1)  # the template's arm
BENCH = parse_bench(TEMPLATE.encode())

# The servo whose parameters the fit does not know: softer, slower to act and
# with more friction than the defaults, and inside the template's limits.
HIDDEN = ServoParams(
    kp_n_m_rad=1.4,
    kd_n_m_s_rad=0.05,
    torque_max_n_m=1.0,
    inertia_kg_m2=0.0016,
    viscous_n_m_s_rad=0.02,
    coulomb_n_m=0.009,
    delay_s=0.008,
)
# What the default experiment set can determine under the template's limits.
DETERMINED = ("kp_n_m_rad", "inertia_kg_m2", "coulomb_n_m", "delay_s", DAMPING_TOTAL)


def bench(**settings: str):
    text = TEMPLATE
    for key, value in settings.items():
        text, count = re.subn(rf"^{key} = .*$", f"{key} = {value}", text, flags=re.MULTILINE)
        assert count == 1, key
    return parse_bench(text.encode())


def characterize(tmp_path, params=DEFAULT_PARAMS, bench_=BENCH, **config):
    servo = SimServo(SimConfig(**config), params=params, load=LOAD)
    return run_characterize(bench_, runs_dir=tmp_path, adapter=servo)


def true_value(params: ServoParams, name: str) -> float:
    if name == DAMPING_TOTAL:
        return params.kd_n_m_s_rad + params.viscous_n_m_s_rad
    return getattr(params, name)


@pytest.fixture(scope="module")
def hidden_quiet(tmp_path_factory):
    """No measurement noise, no timing jitter; the reading is still quantised."""
    path = tmp_path_factory.mktemp("quiet")
    return characterize(path, HIDDEN, noise=False, jitter_s=0.0)


@pytest.fixture(scope="module")
def hidden_noisy(tmp_path_factory):
    """The simulator's default noise and jitter."""
    return characterize(tmp_path_factory.mktemp("noisy"), HIDDEN, seed=4)


@pytest.fixture(scope="module")
def backlash(tmp_path_factory):
    # 0.6 deg of play: about the most that still passes check's direction item.
    path = tmp_path_factory.mktemp("backlash")
    return characterize(path, HIDDEN, seed=4, backlash_rad=0.6 * DEG)


@pytest.fixture(scope="module")
def cli_run(tmp_path_factory):
    """`servobench characterize --json` in a directory holding the template."""
    here = tmp_path_factory.mktemp("cli")
    (here / "bench.toml").write_text(TEMPLATE, encoding="utf-8")
    previous = Path.cwd()
    os.chdir(here)
    try:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = main(["characterize", "--json"])
    finally:
        os.chdir(previous)
    return here, code, json.loads(out.getvalue())


# == The "Done when" checks of plan step 8 ========================================


@pytest.mark.parametrize(
    ("run", "tolerance"), [("hidden_quiet", 0.10), ("hidden_noisy", 0.20)]
)
def test_hidden_parameters_are_recovered_within_the_plan_tolerance(run, tolerance, request):
    result = request.getfixturevalue(run)
    parameters = result.metrics["parameters"]
    # The test must not pass by declaring everything unidentifiable.
    assert [name for name in DETERMINED if parameters[name]["status"] == "identifiable"] == list(
        DETERMINED
    )
    for name, entry in parameters.items():
        if entry["status"] != "identifiable":
            assert entry["value"] is None and entry["uncertainty"] is None
            continue
        true = true_value(HIDDEN, name)
        error = abs(entry["value"] - true) / true
        assert error <= tolerance, (name, entry["value"], true)
        # The stated uncertainty is a real number, not a placeholder.
        assert 0 < entry["uncertainty"] < 0.1 * entry["value"], name


def test_the_fitted_model_predicts_the_held_out_experiment_clearly_better(hidden_noisy):
    validation = hidden_noisy.metrics["validation"]
    fitted = validation["fitted_model"]["rms_error_rad"]
    default = validation["default_model"]["rms_error_rad"]
    assert default >= 3 * fitted
    assert fitted <= GOOD_FIT_NOISE_RATIO * validation["noise_floor_rad"]
    assert validation["experiment"] == "holdout" and validation["fit_quality"] == "good"
    # The held-out experiment really was held out.
    assert set(hidden_noisy.metrics["fit"]["per_experiment"]) == {"steps", "sine", "sweep"}
    # Both numbers, and the noise floor, are in what a reader sees first.
    for value in (fitted, default, validation["noise_floor_rad"]):
        assert f"{math.degrees(value):.3g} deg" in hidden_noisy.summary


def test_an_experiment_set_that_never_saturates_reports_the_torque_limit_not_identifiable(
    hidden_noisy,
):
    fit = hidden_noisy.metrics["fit"]
    assert fit["saturated"] is False
    torque = hidden_noisy.metrics["parameters"]["torque_max_n_m"]
    assert torque["status"] == "not_identifiable" and torque["value"] is None
    assert "never reached its torque limit" in torque["reason"]
    # What is known is a lower bound, and it is true.
    assert 0 < torque["lower_bound"] < HIDDEN.torque_max_n_m
    assert torque["lower_bound"] == pytest.approx(fit["peak_torque_n_m"])
    # Unsaturated, kd and viscous friction are known only as their sum.
    for name in ("kd_n_m_s_rad", "viscous_n_m_s_rad"):
        entry = hidden_noisy.metrics["parameters"][name]
        assert entry["status"] == "not_identifiable" and entry["value"] is None
        assert entry["determined_only_as"] == DAMPING_TOTAL
    assert hidden_noisy.verdict == "partial" and hidden_noisy.exit_code == ExitCode.PASS
    assert hidden_noisy.metrics["parameters_usable"] is True


def test_a_servo_with_backlash_still_gets_a_result_that_does_not_claim_a_good_fit(
    backlash, hidden_noisy
):
    validation = backlash.metrics["validation"]
    assert backlash.verdict == "fail" and backlash.exit_code == ExitCode.FAIL
    assert backlash.needs_human is True
    assert validation["fit_quality"] == "poor"
    assert validation["fitted_error_to_noise_floor"] > GOOD_FIT_NOISE_RATIO
    # Visibly worse than the same servo without backlash.
    good = hidden_noisy.metrics["validation"]["fitted_model"]["rms_error_rad"]
    assert validation["fitted_model"]["rms_error_rad"] > 1.6 * good
    assert backlash.summary.startswith("Fail: the model does not describe this actuator")
    assert "not a failed run" in backlash.next and "Do not rerun" in backlash.next
    # The parameters are still reported, each says it is not a measurement,
    # the export says not to use them, and no MuJoCo fragment is written.
    assert backlash.metrics["parameters_usable"] is False
    for entry in backlash.metrics["parameters"].values():
        if entry["status"] == "identifiable":
            assert entry["reason"].startswith("POOR FIT: ")
    exported = json.loads(Path(backlash.run_dir, JSON_FILE).read_text())
    assert exported["fit_quality"]["usable"] is False
    assert "POOR FIT" in exported["fit_quality"]["statement"]
    assert not Path(backlash.run_dir, MUJOCO_FILE).exists()
    assert exported["mujoco"]["file"] is None


def test_the_command_line_exits_0_and_lists_the_files_it_wrote(cli_run):
    here, code, output = cli_run
    assert code == 0 and output["exit_code"] == 0
    assert output["command"] == "characterize" and output["verdict"] == "partial"
    assert output["needs_human"] is False
    names = {Path(path).name for path in output["files"]}
    described = {Path(path).name: what for path, what in output["files"].items()}
    assert described[JSON_FILE].endswith("for a simulator")
    assert described[MUJOCO_FILE].endswith("its position actuator")
    assert described["holdout_overlay.png"].endswith("with each model's error")
    assert {JSON_FILE, MUJOCO_FILE, "holdout_overlay.png", "run.json"} <= names
    for path in output["files"]:
        assert (here / path).is_file(), path
    assert (here / output["run_dir"] / "holdout_overlay.png").read_bytes()[:8] == (
        b"\x89PNG\r\n\x1a\n"
    )
    # One record for check, one for each experiment, one for the command.
    record = records.load_run(here / output["run_dir"])
    assert record.info["command"] == "characterize" and record.verdict == "partial"
    assert len(record.info["parts"]) == 5
    for part in record.info["parts"]:
        assert records.load_run(here / part).trace is not None


# == The gate: check must pass first ==================================================


@pytest.mark.parametrize(
    ("fault", "item"),
    [
        ("reversed_direction", "direction"),
        ("wrong_zero", "zero"),
        ("overheating", "temperature_rise"),
    ],
)
def test_an_actuator_that_fails_check_is_not_characterized(tmp_path, fault, item):
    result = characterize(tmp_path, faults=frozenset({fault}))
    assert result.verdict == "fail" and result.exit_code == ExitCode.FAIL
    assert result.needs_human is True
    assert result.summary.startswith("Not characterized: check's verdict was fail")
    assert item in result.summary
    assert "runs only on an actuator that passes check" in result.next
    # Nothing but check moved the actuator.
    runs, _ = records.list_runs(tmp_path)
    assert sorted(run.command for run in runs) == ["characterize", "check"]
    assert "parameters" not in result.metrics


def test_a_device_that_stops_answering_keeps_checks_exit_code_and_needs_a_human(tmp_path):
    result = characterize(tmp_path, faults=frozenset({"dropped_replies"}))
    assert result.verdict == "fail" and result.exit_code == ExitCode.FAIL
    assert result.needs_human is True and "bus" in result.summary
    runs, _ = records.list_runs(tmp_path)
    assert sorted(run.command for run in runs) == ["characterize", "check"]


def test_without_a_load_nothing_is_contacted(tmp_path):
    no_load = parse_bench(TEMPLATE.split("[load]")[0].encode() + b"\n[sim]\n")
    servo = SimServo(SimConfig(), load=LOAD)
    calls = []
    servo.read = lambda *a, **k: calls.append("read")  # type: ignore[method-assign]
    result = run_characterize(no_load, runs_dir=tmp_path, adapter=servo)
    assert result.verdict == "refused" and result.exit_code == ExitCode.USAGE
    assert "[load]" in result.summary and calls == []
    assert not tmp_path.exists() or not any(tmp_path.iterdir())


# == Stopping the sequence =========================================================


def test_a_safety_abort_stops_the_sequence_and_nothing_is_fitted(tmp_path):
    # A stiffer, faster servo than the default model: the largest step, sized
    # by the default model with a 20 % margin, still goes past speed_max.
    fast = ServoParams(
        kp_n_m_rad=3.6, kd_n_m_s_rad=0.05, torque_max_n_m=1.0, inertia_kg_m2=0.0012,
        viscous_n_m_s_rad=0.02, coulomb_n_m=0.005, delay_s=0.003,
    )  # fmt: skip
    result = characterize(tmp_path, fast)
    assert result.verdict == "aborted" and result.exit_code == ExitCode.SAFETY
    assert result.needs_human is True
    assert result.summary.startswith("Characterize stopped at experiment 1 of 4 (steps)")
    assert "sine, sweep, holdout not run" in result.summary
    assert "nothing was fitted" in result.summary
    assert list(result.metrics["experiments"]) == ["steps"]
    assert "parameters" not in result.metrics


class _SafeStateFailsAfterCheck(SimServo):
    """The safe state stops being confirmed once a command larger than check's move arrives."""

    big = False

    def command(self, name, value):
        if abs(value) > 5 * DEG:
            self.big = True
        return super().command(name, value)

    def enter_safe_state(self):
        if self.big:
            raise RuntimeError("no acknowledgement")
        super().enter_safe_state()


def test_a_safe_state_not_confirmed_stops_everything(tmp_path):
    servo = _SafeStateFailsAfterCheck(SimConfig(), load=LOAD)
    result = run_characterize(BENCH, runs_dir=tmp_path, adapter=servo)
    assert result.verdict == "safe_state_not_confirmed"
    assert result.exit_code == ExitCode.DEVICE and result.needs_human is True
    assert "SAFE STATE NOT CONFIRMED" in result.summary
    assert list(result.metrics["experiments"]) == ["steps"]


def test_experiments_are_separated_by_the_cool_down(cli_run):
    here, _, output = cli_run
    record = records.load_run(here / output["run_dir"])
    parts = [records.load_run(here / part) for part in record.info["parts"]]
    cooldown_s = BENCH.limits.cooldown_s
    for before, after in zip(parts, parts[1:], strict=False):
        assert after.trace.t_enable_s - before.safe_state.t_s >= cooldown_s - 1e-6


def test_with_when_too_warm_refuse_characterize_waits_the_cool_down_itself(tmp_path):
    # The runner would refuse a run inside the cool-down; characterize waits it
    # out (in the safe state, on the adapter's clock) instead of failing its
    # own second experiment.
    refuse = bench(when_too_warm='"refuse"')
    servo = SimServo(SimConfig(), load=LOAD)
    result = run_characterize(refuse, runs_dir=tmp_path, adapter=servo)
    assert result.verdict == "partial", result.summary
    assert any("waited 60 s for the cool-down" in note for note in result.metrics["notes"])


# == The experiment set ===============================================================


def test_the_default_experiment_set_is_sized_from_the_bench_limits():
    plan = plan_experiments(BENCH, SimServo(SimConfig(), load=LOAD).describe())
    specs = {p.name: p.spec for p in plan.experiments}
    assert [p.role for p in plan.experiments] == ["fit", "fit", "fit", "holdout"]
    steps, sine, sweep, holdout = specs["steps"], specs["sine"], specs["sweep"], specs["holdout"]
    assert isinstance(steps, Step) and isinstance(sine, SteppedSine)
    # Under the template, speed limits the steps: about 11.6 deg, against the
    # 14 deg that the rehearsal alone would allow.
    largest = max(abs(a) for a in steps.amplitudes_rad)
    assert 11 * DEG < largest < 12.5 * DEG
    assert max(abs(a) for a in holdout.amplitudes_rad) < largest
    assert set(holdout.amplitudes_rad).isdisjoint(steps.amplitudes_rad)
    # The sine: commanded speed at most half speed_max; up to rate / 16.
    assert sine.peak_command_speed_rad_s <= 0.5 * BENCH.limits.speed_max_rad_s + 1e-9
    assert max(sine.frequencies_hz) == 8.0
    assert sweep.amplitude_rad == pytest.approx(40 * DEG)
    for spec in specs.values():
        assert spec.check(BENCH.limits) == []
        assert spec.centre_rad == 0.0  # each ends where the arm hangs
        assert spec.planned_duration_s <= 0.9 * BENCH.limits.experiment_max_s


def test_a_faster_speed_limit_gives_larger_steps_and_a_short_experiment_max_is_refused():
    description = SimServo(SimConfig(), load=LOAD).describe()
    wide = plan_experiments(bench(speed_max='"360 deg/s"'), description)
    template = plan_experiments(BENCH, description)
    assert max(wide.experiments[0].spec.amplitudes_rad) > 1.5 * max(
        template.experiments[0].spec.amplitudes_rad
    )
    with pytest.raises(PlanError, match="experiment_max"):
        plan_experiments(bench(experiment_max='"5 s"'), description)


# == The output =====================================================================


def test_every_parameter_has_a_status_and_a_reason_and_nothing_is_filled_in(hidden_noisy):
    parameters = hidden_noisy.metrics["parameters"]
    assert list(parameters) == [*PARAMETERS, DAMPING_TOTAL]
    for name, entry in parameters.items():
        assert entry["status"] in ("identifiable", "not_identifiable")
        assert entry["reason"] and entry["unit"] and entry["meaning"]
        if entry["status"] == "not_identifiable":
            assert entry["value"] is None and entry["uncertainty"] is None, name


def test_the_starting_points_and_their_agreement_are_reported(hidden_noisy):
    fit = hidden_noisy.metrics["fit"]
    assert fit["starts_total"] >= 4
    assert fit["starts_reaching_best"] >= 3
    for name in DETERMINED:
        assert "starting points that reached the best fit agree" in (
            hidden_noisy.metrics["parameters"][name]["reason"]
        )


def test_the_export_holds_conditions_and_never_a_value_for_what_was_not_measured(hidden_noisy):
    exported = json.loads(Path(hidden_noisy.run_dir, JSON_FILE).read_text())
    conditions = exported["conditions"]
    assert conditions["simulated"] is True and conditions["firmware"]
    assert conditions["bench_fingerprint"] == BENCH.fingerprint
    assert conditions["load"]["mass_kg"] == pytest.approx(0.05)
    assert conditions["supply_voltage_v"]["min"] > 6.0
    assert conditions["temperature_degc"]["max"] >= conditions["temperature_degc"]["min"]
    assert exported["tool_version"]
    for name, entry in exported["parameters"].items():
        if entry["status"] == "not_identifiable":
            assert entry["value"] is None and entry["uncertainty"] is None, name
    assert exported["parameters"]["torque_max_n_m"]["lower_bound"] > 0


def test_the_mujoco_fragment_is_well_formed_and_leaves_out_what_was_not_measured(hidden_noisy):
    text = Path(hidden_noisy.run_dir, MUJOCO_FILE).read_text()
    root = ElementTree.fromstring(text)
    assert root.tag == "mujoco"
    default = root.find("default/default")
    joint, position = default.find("joint"), default.find("position")
    parameters = hidden_noisy.metrics["parameters"]
    assert float(joint.get("armature")) == pytest.approx(parameters["inertia_kg_m2"]["value"],
                                                         rel=1e-5)  # fmt: skip
    assert float(position.get("kp")) == pytest.approx(parameters["kp_n_m_rad"]["value"], rel=1e-5)
    # Only the sum of the damping is known: it is on the joint, and kv is not set.
    assert float(joint.get("damping")) == pytest.approx(
        parameters[DAMPING_TOTAL]["value"], rel=1e-5
    )
    assert position.get("kv") is None
    # The torque limit was not measured: no force range.
    assert position.get("forcelimited") == "false" and position.get("forcerange") is None
    assert float(position.get("delay")) == pytest.approx(parameters["delay_s"]["value"], rel=1e-5)
    # The history buffer covers the delay down to a 0.5 ms timestep.
    assert int(position.get("nsample")) >= parameters["delay_s"]["value"] / 0.0005 + 1


def test_the_text_output_lists_each_parameter(cli_run, capsys):
    here, _, output = cli_run
    from servobench.cli import _text
    from servobench.commands import CommandResult

    text = _text(CommandResult(**{k: output[k] for k in (
        "command", "verdict", "summary", "needs_human", "next", "metrics", "files", "run_dir"
    )}, exit_code=ExitCode(output["exit_code"])))  # fmt: skip
    assert "torque_max_n_m" in text and "not identifiable; at least" in text
    assert "held-out RMS error: fitted" in text
