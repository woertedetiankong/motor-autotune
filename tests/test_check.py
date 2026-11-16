"""The check command on the simulated servo, through the command line and the library."""

from __future__ import annotations

import dataclasses
import json
import math
import re

import pytest

from servobench import records
from servobench.adapters.sim import SimConfig, SimServo
from servobench.cli import main
from servobench.commands import ExitCode, judge_run
from servobench.commands import check as check_module
from servobench.commands.check import MOTION_ITEMS, run_check
from servobench.contract import Fault, NoReply
from servobench.experiments import monitor as checks
from servobench.experiments import runner
from servobench.experiments.runner import Outcome, run_experiment
from servobench.experiments.spec import Hold, Step
from servobench.limits import TEMPLATE, parse_bench
from servobench.model import PendulumLoad

DEG = math.radians(1.0)
LOAD = PendulumLoad(mass_kg=0.05, com_distance_m=0.1)
DEVICE_CALLS = ("read", "read_faults", "enable", "command", "enter_safe_state", "write_parameter")


def bench_text(faults=(), **settings: str) -> str:
    """The template with some settings replaced (TOML text) and faults injected."""
    text = TEMPLATE
    for key, value in settings.items():
        text, count = re.subn(rf"^{key} = .*$", f"{key} = {value}", text, flags=re.MULTILINE)
        assert count == 1, key
    if faults:
        text = text.replace('# faults = ["wrong_zero"]', f"faults = {json.dumps(list(faults))}")
    return text


@pytest.fixture
def here(tmp_path, monkeypatch):
    """Run in an empty directory, as a user would."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


def check(here, capsys, *args, faults=(), text: str | None = None, **settings):
    """Write bench.toml, run `servobench check --json ...`, return (exit code, JSON)."""
    (here / "bench.toml").write_text(text or bench_text(faults, **settings), encoding="utf-8")
    code = main(["check", "--json", *args])
    out = capsys.readouterr().out
    return code, json.loads(out)  # stdout is exactly one JSON object


def statuses(output) -> dict[str, str]:
    return {name: item["status"] for name, item in output["metrics"]["items"].items()}


class Spy(SimServo):
    """The simulated servo, recording every device call."""

    def __init__(self, *faults, **config):
        super().__init__(SimConfig(faults=frozenset(faults), **config), load=LOAD)
        self.calls: list[str] = []

    def __getattribute__(self, name):
        if name in DEVICE_CALLS:
            object.__getattribute__(self, "calls").append(name)
        return object.__getattribute__(self, name)


BENCH = parse_bench(TEMPLATE.encode())


# == The "Done when" checks of plan step 7 =======================================


def test_a_healthy_servo_passes(here, capsys):
    code, output = check(here, capsys)
    assert code == 0
    assert output["verdict"] == "pass" and output["exit_code"] == 0
    assert output["needs_human"] is False and output["next"] is None
    assert list(output)[0] == "verdict"
    found = statuses(output)
    assert found.pop("protections") == "warning"  # the simulated servo has no speed limit etc.
    assert set(found.values()) == {"pass"}
    assert "Warning:" in output["summary"] and "speed limit" in output["summary"]
    # The record: written through records.py, with the items under metrics.
    run_dir = here / output["run_dir"]
    record = records.load_run(run_dir)
    assert record.verdict == "pass" and record.outcome is Outcome.COMPLETED
    assert record.metrics["items"] == output["metrics"]["items"]
    assert record.metrics["step_response"]["analysis"] == "step_response"
    assert record.info["summary"] == output["summary"]
    for path in output["files"]:
        assert (here / path).is_file(), path
    # The limits in force are printed with the result.
    assert output["metrics"]["limits"]["speed_max_rad_s"] == pytest.approx(math.pi)
    assert output["metrics"]["bench"]["fingerprint"] == record.info["bench"]["fingerprint"]


@pytest.mark.parametrize(
    ("fault", "item", "advice"),
    [
        ("reversed_direction", "direction", "direction setting or its wiring is reversed"),
        ("wrong_zero", "zero", "zero offset is wrong"),
        ("overheating", "temperature_rise", "Let it cool"),
        ("dropped_replies", "bus", "check the cable"),
    ],
)
def test_each_injected_fault_fails_and_names_its_item(here, capsys, fault, item, advice):
    code, output = check(here, capsys, faults=[fault])
    assert code == 1, output["summary"]
    assert output["verdict"] == "fail"
    assert output["metrics"]["by_status"]["fail"] == [item]
    assert output["summary"].startswith(f"Fail: {item}:")
    assert advice in output["next"] and output["next"].startswith(f"{item}:")
    assert output["needs_human"] is True
    assert records.load_run(here / output["run_dir"]).verdict == "fail"


def test_each_fault_is_found_for_the_right_reason(here, capsys):
    _, reversed_ = check(here, capsys, faults=["reversed_direction"])
    steps = reversed_["metrics"]["items"]["direction"]["values"]["steps"]
    assert [step["step_ratio"] < -0.9 for step in steps] == [True, True]
    assert reversed_["metrics"]["run"]["outcome"] == "completed"  # the run was not aborted

    _, zero = check(here, capsys, faults=["wrong_zero"])
    values = zero["metrics"]["items"]["zero"]["values"]
    assert values["position_at_rest_rad"] == pytest.approx(20 * DEG, abs=0.2 * DEG)
    assert statuses(zero)["direction"] == "pass"  # it moves normally

    _, hot = check(here, capsys, faults=["overheating"])
    rise = hot["metrics"]["items"]["temperature_rise"]["values"]["temperature_rise_degc"]
    assert rise > 5.0  # against temperature_rise_max 3 degC; a healthy servo stays under 0.5
    assert statuses(hot)["temperature"] == "pass"  # it starts cool

    _, drops = check(here, capsys, faults=["dropped_replies"])
    bus = drops["metrics"]["items"]["bus"]["values"]
    assert bus["missed"] > 20 and bus["answered_rate_hz"] < 100.0
    # On a bus that drops replies, check does not move the actuator at all.
    assert drops["metrics"]["motion"] == "not run: a read-only item did not pass"
    assert {statuses(drops)[name] for name in MOTION_ITEMS} == {"skipped"}
    assert drops["metrics"]["run"] is None


def test_no_motion_reports_the_moving_items_as_skipped_not_passed(here, capsys):
    code, output = check(here, capsys, "--no-motion")
    assert code == 0
    assert output["verdict"] == "partial"
    assert statuses(output)["direction"] == "skipped"
    assert statuses(output)["temperature_rise"] == "skipped"
    assert output["metrics"]["by_status"]["skipped"] == ["direction", "temperature_rise"]
    assert "skipped (--no-motion)" in output["summary"] and "not everything" in output["summary"]
    assert "without --no-motion" in output["next"]
    record = records.load_run(here / output["run_dir"])
    assert record.outcome is None and record.trace is None and record.verdict == "partial"
    assert len(record.readings) > 100


def test_no_motion_never_changes_the_actuator(tmp_path):
    servo = Spy()
    result = run_check(BENCH, motion=False, adapter=servo, runs_dir=tmp_path)
    assert result.verdict == "partial"
    assert set(servo.calls) == {"read", "read_faults"}


def test_no_motion_still_finds_a_wrong_zero(here, capsys):
    code, output = check(here, capsys, "--no-motion", faults=["wrong_zero"])
    assert code == 1 and output["metrics"]["by_status"]["fail"] == ["zero"]


def test_a_missing_limit_is_a_usage_error_naming_the_field(here, capsys):
    text = re.sub(r"^speed_max = .*\n", "", TEMPLATE, flags=re.MULTILINE)
    code, output = check(here, capsys, text=text)
    assert code == 2
    assert output["verdict"] == "error"
    assert "[limits] speed_max is missing" in output["summary"]
    assert output["metrics"]["problems"] == [
        'bench.toml: [limits] speed_max is missing; expected an angular velocity, for example'
        ' "90 deg/s"'
    ]
    assert "Do not write limit values" in output["next"]
    assert not (here / "runs").exists()  # nothing was contacted, nothing recorded


def test_a_missing_limit_in_text_mode(here, capsys):
    text = re.sub(r"^cooldown = .*\n", "", TEMPLATE, flags=re.MULTILINE)
    (here / "bench.toml").write_text(text, encoding="utf-8")
    assert main(["check"]) == 2
    err = capsys.readouterr().err
    assert err.startswith("verdict: error (exit 2)")
    assert "[limits] cooldown is missing" in err


# == The other faults ==============================================================


def test_a_stuck_position_reading_fails_direction(here, capsys):
    code, output = check(here, capsys, faults=["stuck_position"])
    assert code == 1
    assert output["metrics"]["by_status"]["fail"] == ["direction"]
    assert "did not change at all" in output["metrics"]["items"]["direction"]["detail"]
    assert "stuck" in output["next"]


def test_a_runaway_is_a_safety_abort_and_a_stop(here, capsys):
    code, output = check(here, capsys, faults=["runaway"])
    assert code == 3
    assert output["verdict"] == "aborted" and output["needs_human"] is True
    assert output["metrics"]["run"]["stop_kind"] in (checks.SPEED_LIMIT, checks.POSITION_LIMIT)
    assert output["metrics"]["run"]["safe_state"] == "confirmed"
    assert {statuses(output)[name] for name in MOTION_ITEMS} == {"unknown"}
    assert "run nothing else" in output["next"]
    record = records.load_run(here / output["run_dir"])
    assert record.outcome is Outcome.ABORTED and record.verdict == "aborted"


# == Other findings ===================================================================


def test_a_device_fault_is_a_device_error_and_nothing_moves(tmp_path):
    class Faulted(Spy):
        def read_faults(self):
            super().read_faults()
            return (Fault("overheat", "temperature reached the cutoff"),)

    servo = Faulted()
    result = run_check(BENCH, adapter=servo, runs_dir=tmp_path)
    assert result.verdict == "fail" and result.exit_code == ExitCode.DEVICE
    assert result.metrics["by_status"]["fail"] == ["faults"]
    assert "cannot clear faults" in result.next
    assert "enable" not in servo.calls and "command" not in servo.calls


def test_an_actuator_that_does_not_respond_is_a_device_error(tmp_path):
    class Silent(Spy):
        def read(self, signals=None):
            raise NoReply("no reply")

    servo = Silent()
    result = run_check(BENCH, adapter=servo, runs_dir=tmp_path)
    assert result.verdict == "error" and result.exit_code == ExitCode.DEVICE
    assert result.needs_human
    items = result.metrics["items"]
    assert items["responds"]["status"] == "fail" and "NoReply" in items["responds"]["detail"]
    assert {item["status"] for name, item in items.items() if name != "responds"} == {"skipped"}
    assert "enable" not in servo.calls
    assert records.load_run(result.run_dir).verdict == "error"  # still recorded


def test_the_wrong_device_id_fails_identity_and_nothing_moves(here, capsys):
    text = bench_text().replace("# id = 1", "id = 2")
    code, output = check(here, capsys, text=text)
    assert code == 1
    assert output["metrics"]["by_status"]["fail"] == ["identity"]
    assert "has id 1, but bench.toml names id 2" in output["summary"]
    assert {statuses(output)[name] for name in MOTION_ITEMS} == {"skipped"}


def test_a_warm_actuator_fails_temperature_and_waiting_is_enough(here, capsys):
    warm = 'initial_temperature = "50 degC"'
    text = bench_text().replace('# initial_temperature = "25 degC"', warm)
    code, output = check(here, capsys, text=text)
    assert code == 1
    assert output["metrics"]["by_status"]["fail"] == ["temperature"]
    assert output["needs_human"] is False and "Let the actuator cool" in output["next"]


def test_without_a_load_the_zero_is_unknown_and_the_verdict_partial(here, capsys):
    text = TEMPLATE[: TEMPLATE.index("[load]")] + TEMPLATE[TEMPLATE.index("[sim]") :]
    code, output = check(here, capsys, text=text)
    assert code == 0
    assert output["verdict"] == "partial"
    assert statuses(output)["zero"] == "unknown"
    assert statuses(output)["direction"] == "pass"
    assert "[load]" in output["next"]


# == Runs that do not complete ============================================================


def test_a_check_refused_at_validation_is_a_usage_error_without_a_traceback(here, capsys):
    code, output = check(here, capsys, experiment_max='"2 s"')  # check's moves take 2.5 s
    assert code == 2
    assert output["verdict"] == "refused"
    assert output["metrics"]["run"]["stop_kind"] == runner.SPEC_INVALID
    assert "experiment_max" in output["summary"]
    assert {statuses(output)[name] for name in MOTION_ITEMS} == {"unknown"}
    record = records.load_run(here / output["run_dir"])
    assert record.trace is None and record.outcome is Outcome.REFUSED


def test_a_check_refused_in_rehearsal_is_a_safety_refusal(here, capsys):
    code, output = check(here, capsys, speed_max='"25 deg/s"', approach_speed='"20 deg/s"')
    assert code == 3
    assert output["verdict"] == "refused" and output["needs_human"] is True
    assert output["metrics"]["run"]["stop_kind"] == runner.REHEARSAL_FAILED
    assert "do not change the bench file" in output["next"]


def test_safe_state_not_confirmed_cannot_be_mistaken(capsys, tmp_path):
    class Deaf(Spy):
        def enter_safe_state(self):
            super().enter_safe_state()
            raise NoReply("no answer")

    result = run_check(BENCH, adapter=Deaf(), runs_dir=tmp_path)
    assert result.verdict == "safe_state_not_confirmed"
    assert result.exit_code == ExitCode.DEVICE and result.needs_human
    assert result.summary.startswith("SAFE STATE NOT CONFIRMED")
    assert "cut its power" in result.next
    # Moves that completed are still not judged when the actuator may be live.
    assert {result.metrics["items"][name]["status"] for name in MOTION_ITEMS} == {"unknown"}
    assert records.load_run(result.run_dir).verdict == "safe_state_not_confirmed"


def test_a_runner_cool_down_holds_across_invocations(here, capsys):
    first_code, first = check(here, capsys, when_too_warm='"refuse"')
    assert first_code == 0 and first["metrics"]["cooldown"]["previous_run"] is None
    code, second = check(here, capsys, when_too_warm='"refuse"')
    assert code == 3
    assert second["verdict"] == "refused"
    assert second["metrics"]["run"]["stop_kind"] == runner.COOLDOWN
    assert second["needs_human"] is False  # waiting is all it takes
    assert "Wait" in second["next"] and "Try again in" in second["next"]
    assert second["metrics"]["cooldown"]["previous_run"] == first["run_dir"]
    assert 55.0 < second["metrics"]["cooldown"]["rest_left_s"] <= 60.0


def test_with_wait_the_cool_down_is_waited_out(here, capsys):
    check(here, capsys)
    code, second = check(here, capsys)
    assert code == 0
    rest_s = second["metrics"]["cooldown"]["rest_left_s"]
    assert rest_s > 55.0
    assert second["metrics"]["run"]["cooled_s"] == pytest.approx(rest_s, abs=0.5)
    assert any("virtual clock" in note for note in second["metrics"]["notes"])


def test_records_that_do_not_count_for_the_cool_down(here, capsys):
    refuse = {"when_too_warm": '"refuse"'}
    check(here, capsys, "--no-motion", **refuse)  # never enabled the actuator
    code, output = check(here, capsys, **refuse)
    assert code == 0, output["summary"]
    # A record of another actuator does not count either.
    run_json = here / output["run_dir"] / records.RUN_FILE
    info = json.loads(run_json.read_text(encoding="utf-8"))
    info["target"]["device_id"] = "7"
    run_json.write_text(json.dumps(info), encoding="utf-8")
    code, output = check(here, capsys, **refuse)
    assert code == 0, output["summary"]


# == The mapping of runner outcomes ========================================================


def outcome_of(spec, *faults, bench=BENCH, servo=None, **config):
    servo = servo or SimServo(SimConfig(faults=frozenset(faults), **config), load=LOAD)
    return run_experiment(servo, bench, spec)


def test_every_runner_outcome_has_a_verdict_and_exit_code():
    completed = outcome_of(Hold(0.0, 1.0))
    assert completed.outcome is Outcome.COMPLETED and judge_run(completed) is None

    cases = []
    cases.append((outcome_of(Hold(85 * DEG, 1.0)), "refused", 2, True))  # spec_invalid
    cases.append((outcome_of(Step(0.0, (30 * DEG,), 1.0, 0.5)), "refused", 3, True))  # rehearsal
    cases.append(
        (outcome_of(Hold(0.0, 1.0), initial_temperature_degc=65.0), "refused", 3, True)
    )  # above temperature_max at preflight
    refusing = parse_bench(bench_text(when_too_warm='"refuse"').encode())
    too_warm = outcome_of(Hold(0.0, 1.0), bench=refusing, initial_temperature_degc=50.0)
    assert too_warm.stop.kind == runner.TOO_WARM
    cases.append((too_warm, "refused", 3, True))
    cases.append((outcome_of(Hold(0.0, 25.0), "overheating"), "aborted", 3, True))

    class Faulted(SimServo):
        def read_faults(self):
            return (Fault("overheat", "too hot"),)

    class Silent(SimServo):
        def read(self, signals=None):
            raise NoReply("no reply")

    class Broken(SimServo):
        def command(self, name, value):
            raise RuntimeError("bug")

    class Deaf(SimServo):
        def enter_safe_state(self):
            raise NoReply("no answer")

    cases.append((outcome_of(Hold(0.0, 1.0), servo=Faulted(load=LOAD)), "refused", 4, True))
    cases.append((outcome_of(Hold(0.0, 1.0), servo=Silent(load=LOAD)), "refused", 4, True))
    cases.append((outcome_of(Hold(0.0, 1.0), servo=Broken(load=LOAD)), "error", 4, True))
    cases.append(
        (outcome_of(Hold(0.0, 1.0), servo=Deaf(load=LOAD)), "safe_state_not_confirmed", 4, True)
    )
    interrupted = dataclasses.replace(
        completed,
        outcome=Outcome.INTERRUPTED,
        stop=runner.StopReason(runner.INTERRUPTED_KIND, runner.RUN, "interrupted by Ctrl-C"),
    )
    cases.append((interrupted, "aborted", 3, True))
    expected_outcomes = {result.outcome for result, *_ in cases} | {Outcome.COMPLETED}
    assert expected_outcomes == set(Outcome)  # every outcome is covered

    for result, verdict, code, human in cases:
        judgement = judge_run(result)
        label = f"{result.outcome.value}/{result.stop and result.stop.kind}"
        assert (judgement.verdict, int(judgement.exit_code), judgement.needs_human) == (
            verdict,
            code,
            human,
        ), label
        assert judgement.next, label
        assert verdict in records.VERDICTS


def test_a_device_fault_in_the_middle_of_a_run_is_a_device_error():
    class FaultsLater(SimServo):
        polls = 0

        def read_faults(self):
            self.polls += 1
            if self.polls > 2:
                return (Fault("overload", "over its torque limit"),)
            return super().read_faults()

    result = outcome_of(Hold(0.0, 3.0), servo=FaultsLater(load=LOAD))
    assert result.outcome is Outcome.ABORTED and result.stop.kind == checks.DEVICE_FAULT
    judgement = judge_run(result)
    assert (judgement.verdict, judgement.exit_code) == ("aborted", ExitCode.DEVICE)
    assert "cannot clear faults" in judgement.next


# == Errors ================================================================================


def test_an_unexpected_exception_after_the_run_is_reported_with_its_record(
    here, capsys, monkeypatch
):
    def broken(trace, spec):
        raise ZeroDivisionError("in the analysis")

    monkeypatch.setattr(check_module, "analyse_step", broken)
    code, output = check(here, capsys)
    assert code == 4
    assert output["verdict"] == "error" and output["needs_human"] is True
    assert output["error"]["type"] == "ZeroDivisionError"
    assert "analyse_step" not in output["error"]["message"]
    assert "Traceback" in output["error"]["traceback"]
    assert "Completed" in output["next"]  # says what the runner reported before the error
    record = records.load_run(here / output["run_dir"])
    assert record.verdict == "error" and record.safe_state.status.value == "confirmed"
    assert record.metrics["error"]["type"] == "ZeroDivisionError"
