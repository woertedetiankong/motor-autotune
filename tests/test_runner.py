"""The experiment runner: rehearsal, safety monitor, thermal budget, safe state on every exit."""

from __future__ import annotations

import ast
import dataclasses
import inspect
import math
import os
import re
import signal
import time
from pathlib import Path

import pytest

import servobench
from servobench.adapters.sim import SimConfig, SimServo
from servobench.contract import (
    POSITION,
    TEMPERATURE,
    CommandRejected,
    CommunicationError,
    NoReply,
)
from servobench.experiments import monitor as checks
from servobench.experiments import runner
from servobench.experiments.monitor import Monitor
from servobench.experiments.runner import (
    Outcome,
    SafeState,
    run_experiment,
)
from servobench.experiments.spec import Hold, Segment, Step, SteppedSine, command_band_rad
from servobench.limits import TEMPLATE, parse_bench
from servobench.model import DEFAULT_PARAMS, PendulumLoad

DEG = math.radians(1.0)
LOAD = PendulumLoad(mass_kg=0.05, com_distance_m=0.1)  # the template's arm
SMALL_STEP = Step(centre_rad=0.0, amplitudes_rad=(10 * DEG,), dwell_s=1.0, settle_s=0.5)
DEVICE_CALLS = ("read", "read_faults", "enable", "command", "enter_safe_state", "write_parameter")


def bench(**settings: str):
    """The template bench file with some settings replaced, each given as TOML text."""
    text = TEMPLATE
    for key, value in settings.items():
        text, count = re.subn(rf"^{key} = .*$", f"{key} = {value}", text, flags=re.MULTILINE)
        assert count == 1, key
    return parse_bench(text.encode())


TEMPLATE_BENCH = bench()


class Spy(SimServo):
    """The simulated servo, recording every device call, with hooks to make calls fail.

    ``hooks[name](servo, n)`` runs before the n-th call of ``name`` and may raise.
    """

    def __init__(self, *faults, hooks=None, **config):
        super().__init__(SimConfig(faults=frozenset(faults), **config), load=LOAD)
        self.calls: list[str] = []
        self.hooks = hooks or {}

    def _call(self, name):
        self.calls.append(name)
        hook = self.hooks.get(name)
        if hook is not None:
            hook(self, self.calls.count(name))

    def read(self, signals=None):
        self._call("read")
        return super().read(signals)

    def read_faults(self):
        self._call("read_faults")
        return super().read_faults()

    def enable(self):
        self._call("enable")
        super().enable()

    def command(self, name, value):
        self._call("command")
        return super().command(name, value)

    def enter_safe_state(self):
        self._call("enter_safe_state")
        super().enter_safe_state()

    def write_parameter(self, name, value, *, confirmation=None):
        self._call("write_parameter")
        super().write_parameter(name, value, confirmation=confirmation)


def on_call(number, error):
    """A hook that raises ``error`` on the ``number``-th call."""

    def hook(servo, n):
        if n == number:
            raise error

    return hook


def assert_ends_safe(servo: Spy, result):
    """The run ended with the safe state entered after the last enable, and confirmed."""
    assert result.safe_state.status is SafeState.CONFIRMED
    assert result.safe_state_confirmed
    assert "enter_safe_state" in servo.calls
    last_safe = len(servo.calls) - 1 - servo.calls[::-1].index("enter_safe_state")
    if "enable" in servo.calls:
        assert last_safe > len(servo.calls) - 1 - servo.calls[::-1].index("enable")
    assert "command" not in servo.calls[last_safe:]
    # Through the contract: after the safe state, commands are refused until enable.
    with pytest.raises(CommandRejected):
        SimServo.command(servo, POSITION, 0.0)


def run(servo, spec=SMALL_STEP, bench_file=TEMPLATE_BENCH, **kwargs):
    return run_experiment(servo, bench_file, spec, **kwargs)


# == The "Done when" checks of plan step 4 ======================================


def test_an_overheating_servo_is_stopped_at_the_temperature_limit():
    servo = Spy("overheating")
    result = run(servo, Hold(0.0, 25.0))
    assert result.outcome is Outcome.ABORTED
    assert result.stop.kind == checks.TEMPERATURE_LIMIT
    assert result.stop.limit == 60.0
    temperatures = [sample[TEMPERATURE] for sample in result.trace.samples]
    assert 60.0 < temperatures[-1] < 60.5  # stopped at the limit, not later
    assert max(temperatures[:-1]) <= 60.0
    assert result.faults_after == ()  # before the device's own 70 degC cutoff
    assert result.rehearsal.stop is None  # a healthy servo stays cool
    assert_ends_safe(servo, result)


def test_a_runaway_is_stopped_at_the_speed_limit():
    servo = Spy("runaway")
    result = run(servo, Hold(0.0, 5.0))
    assert result.outcome is Outcome.ABORTED
    assert result.stop.kind == checks.SPEED_LIMIT
    assert result.stop.elapsed_s < 0.05
    assert abs(result.trace.samples[-1][POSITION]) < 5 * DEG
    assert_ends_safe(servo, result)


def test_a_runaway_is_stopped_at_the_travel_limit_when_the_other_checks_allow_more():
    # Each layer on its own: with a far higher speed limit the follow check
    # stops it; with that loosened too, the travel limit does.
    fast = bench(speed_max='"5000 deg/s"')
    result = run(Spy("runaway"), Hold(0.0, 5.0), fast)
    assert result.stop.kind == checks.NOT_FOLLOWING

    lax = bench(speed_max='"5000 deg/s"', follow_error_max='"170 deg"')
    servo = Spy("runaway")
    result = run(servo, Hold(0.0, 5.0), lax)
    assert result.outcome is Outcome.ABORTED
    assert result.stop.kind == checks.POSITION_LIMIT
    positions = [sample[POSITION] for sample in result.trace.samples]
    assert positions[-1] > 90 * DEG and max(positions[:-1]) <= 90 * DEG
    assert_ends_safe(servo, result)


def test_dropped_replies_beyond_the_allowed_count_stop_the_run():
    servo = Spy("dropped_replies")
    result = run(servo, Hold(0.0, 5.0))
    assert result.outcome is Outcome.ABORTED
    assert result.stop.kind == checks.MISSED_REPLIES
    assert result.stop.value == 4 and result.stop.limit == 3
    # The last four device replies the runner waited for were all missed.
    assert len(result.trace.missed) >= 4
    assert_ends_safe(servo, result)

    # With more allowed, the same faulty servo completes the experiment.
    servo = Spy("dropped_replies")
    result = run(servo, Hold(0.0, 5.0), bench(missed_replies_max="50"))
    assert result.outcome is Outcome.COMPLETED
    assert len(result.trace.missed) > 50  # many, but never more than 50 in a row
    assert_ends_safe(servo, result)


class Boom(RuntimeError):
    pass


@pytest.mark.parametrize("where", ["adapter read", "adapter command", "monitor", "between samples"])
def test_an_exception_in_the_middle_of_a_run_still_ends_in_the_safe_state(where, monkeypatch):
    hooks = {}
    if where == "adapter read":
        hooks["read"] = on_call(100, Boom("the adapter broke"))
    elif where == "adapter command":
        hooks["command"] = on_call(3, Boom("the adapter broke"))
    servo = Spy(hooks=hooks)
    count = {"n": 0}

    def in_real_run_on_call(number):
        """True on the ``number``-th call made after the target was enabled."""
        if "enable" not in servo.calls:
            return False  # still preflight or rehearsal
        count["n"] += 1
        return count["n"] == number

    if where == "monitor":
        real_on_sample = Monitor.on_sample

        def on_sample(self, sample):
            if in_real_run_on_call(50):
                raise Boom("the monitor broke")
            return real_on_sample(self, sample)

        monkeypatch.setattr(Monitor, "on_sample", on_sample)
    elif where == "between samples":
        real_setpoint = Segment.setpoint_rad

        def setpoint(self, tau_s):
            if in_real_run_on_call(2):
                raise Boom("the code between samples broke")
            return real_setpoint(self, tau_s)

        monkeypatch.setattr(Segment, "setpoint_rad", setpoint)

    result = run(servo)
    assert result.outcome is Outcome.ERROR
    assert result.stop.kind == runner.EXCEPTION
    assert "broke" in result.stop.message
    assert result.stop.stage == runner.RUN
    assert "enable" in servo.calls
    assert result.trace.samples  # what happened up to the exception is kept
    assert_ends_safe(servo, result)


def test_a_spec_whose_commanded_range_exceeds_the_travel_limit_is_refused_before_any_command():
    servo = Spy()
    spec = Step(0.0, (85 * DEG,), 1.0, 0.5)  # travel is 90 deg, the command range 80 deg
    result = run(servo, spec)
    assert result.outcome is Outcome.REFUSED
    assert result.stop.kind == runner.SPEC_INVALID
    assert result.stop.stage == runner.VALIDATE
    assert "outside the allowed command range" in result.stop.message
    assert servo.calls == []  # not contacted at all, not even read
    assert result.safe_state.status is SafeState.NOT_CONTACTED
    assert result.rehearsal is None and result.trace is None


def test_a_spec_that_passes_validation_but_fails_rehearsal_is_refused_before_touching_it():
    servo = Spy()
    spec = Step(0.0, (30 * DEG,), 1.0, 0.5)  # inside the command range, but too fast
    assert spec.check(TEMPLATE_BENCH.limits) == []
    result = run(servo, spec)
    assert result.outcome is Outcome.REFUSED
    assert result.stop.kind == runner.REHEARSAL_FAILED
    assert result.stop.stage == runner.REHEARSAL
    # Which limit, and when.
    assert result.stop.rehearsal.kind == checks.SPEED_LIMIT
    assert result.stop.rehearsal.elapsed_s == pytest.approx(0.53, abs=0.05)
    assert "speed" in result.stop.message and "0.5" in result.stop.message
    assert "during 'step 1: 0 deg to 30 deg'" in result.stop.message
    # The target was read, and put in (kept in) its safe state; nothing else.
    assert set(servo.calls) <= {"read", "read_faults", "enter_safe_state"}
    assert result.trace is None
    assert result.rehearsal.samples and result.rehearsal.stop.kind == checks.SPEED_LIMIT


@pytest.mark.parametrize("policy", ["wait", "refuse"])
def test_a_second_run_while_the_servo_is_too_warm_waits_or_refuses_as_configured(policy):
    no_rest = bench(cooldown='"0 s"', when_too_warm=f'"{policy}"')
    servo = Spy("overheating")
    first = run(servo, Hold(0.0, 8.0), no_rest)  # warms it to about 50 degC
    assert first.outcome is Outcome.COMPLETED
    assert first.trace.samples[-1][TEMPERATURE] > 45.0
    calls_before = len(servo.calls)

    second = run(servo, Hold(0.0, 2.0), no_rest)
    second_calls = servo.calls[calls_before:]
    if policy == "refuse":
        assert second.outcome is Outcome.REFUSED
        assert second.stop.kind == runner.TOO_WARM
        assert "above start_temperature_max 45 degC" in second.stop.message
        assert "enable" not in second_calls
        assert second.cooled_s == 0.0
    else:
        assert second.outcome is Outcome.COMPLETED
        assert second.cooled_s > 5.0
        assert second.preflight[TEMPERATURE] <= 45.0  # it started only once cool enough
        # It waited at rest in the safe state before enabling.
        assert second_calls.index("enter_safe_state") < second_calls.index("enable")
    assert_ends_safe(servo, second)


# == More on the thermal budget ===============================================


@pytest.mark.parametrize("policy", ["wait", "refuse"])
def test_a_second_run_inside_the_cool_down_waits_or_refuses(policy):
    settings = bench(when_too_warm=f'"{policy}"')
    servo = Spy()
    assert run(servo, Hold(0.0, 1.0), settings).outcome is Outcome.COMPLETED
    end_s = servo.clock.now_s()
    second = run(servo, Hold(0.0, 1.0), settings)
    if policy == "refuse":
        assert second.outcome is Outcome.REFUSED
        assert second.stop.kind == runner.COOLDOWN
        assert second.trace is None
    else:
        assert second.outcome is Outcome.COMPLETED
        assert second.trace.t_enable_s - end_s >= 60.0  # the bench file's cool-down
        assert second.cooled_s == pytest.approx(60.0, abs=0.1)


def test_a_servo_that_does_not_cool_within_cool_wait_max_is_refused():
    servo = Spy(initial_temperature_degc=55.0)
    result = run(servo, Hold(0.0, 1.0), bench(cool_wait_max='"5 s"'))
    assert result.outcome is Outcome.REFUSED
    assert result.stop.kind == runner.TOO_WARM
    assert "within cool_wait_max 5 s" in result.stop.message
    assert result.cooled_s == pytest.approx(5.0, abs=1.1)
    assert "enable" not in servo.calls
    assert_ends_safe(servo, result)


def test_the_cool_down_is_kept_per_adapter_object():
    first = Spy()
    assert run(first, Hold(0.0, 1.0)).outcome is Outcome.COMPLETED
    assert run(Spy(), Hold(0.0, 1.0)).cooled_s == 0.0


# == The safe state itself ======================================================


def test_a_safe_state_that_cannot_be_confirmed_is_reported_loudly_and_not_as_an_abort():
    servo = Spy(hooks={"enter_safe_state": lambda s, n: _raise(NoReply("no answer"))})
    result = run(servo, Hold(0.0, 1.0))
    assert result.outcome is Outcome.SAFE_STATE_NOT_CONFIRMED
    assert result.outcome is not Outcome.ABORTED
    assert not result.safe_state_confirmed
    assert result.safe_state.status is SafeState.NOT_CONFIRMED
    assert result.safe_state.attempts == runner.SAFE_STATE_ATTEMPTS
    assert len(result.safe_state.errors) >= runner.SAFE_STATE_ATTEMPTS
    assert result.stop is None  # the experiment itself completed
    assert result.summary.startswith("SAFE STATE NOT CONFIRMED")
    assert "cut its power now" in result.summary
    assert servo.calls.count("enter_safe_state") == runner.SAFE_STATE_ATTEMPTS


def test_a_safe_state_failure_after_an_abort_keeps_the_abort_reason():
    servo = Spy("runaway", hooks={"enter_safe_state": lambda s, n: _raise(NoReply("gone"))})
    result = run(servo, Hold(0.0, 1.0))
    assert result.outcome is Outcome.SAFE_STATE_NOT_CONFIRMED
    assert result.stop.kind == checks.SPEED_LIMIT
    assert result.summary.startswith("SAFE STATE NOT CONFIRMED")
    assert "speed" in result.summary


def test_a_safe_state_that_fails_once_is_retried_and_confirmed():
    servo = Spy(hooks={"enter_safe_state": on_call(1, NoReply("one lost packet"))})
    result = run(servo, Hold(0.0, 1.0))
    assert result.outcome is Outcome.COMPLETED
    assert result.safe_state.attempts == 2
    assert "one lost packet" in result.safe_state.errors[0]
    assert_ends_safe(servo, result)


def _raise(error):
    raise error


# == Ctrl-C ====================================================================


def test_ctrl_c_raised_inside_the_adapter_ends_in_the_safe_state():
    servo = Spy(hooks={"read": on_call(50, KeyboardInterrupt())})
    result = run(servo)
    assert result.outcome is Outcome.INTERRUPTED
    assert result.stop.kind == runner.INTERRUPTED_KIND
    assert_ends_safe(servo, result)


def test_a_real_ctrl_c_stops_at_the_next_sample_without_tearing_a_read():
    def press_ctrl_c(servo, n):
        if n == 60:
            os.kill(os.getpid(), signal.SIGINT)

    servo = Spy(hooks={"read": press_ctrl_c})
    before = signal.getsignal(signal.SIGINT)
    result = run(servo)
    assert result.outcome is Outcome.INTERRUPTED
    assert result.stop.stage == runner.RUN
    # The read during which Ctrl-C arrived finished and was recorded: reads 2
    # to 60 (read 1 was the preflight) and nothing after.
    assert len(result.trace.samples) == 59
    assert_ends_safe(servo, result)
    assert signal.getsignal(signal.SIGINT) is before  # the handler is put back


def test_ctrl_c_while_waiting_to_cool_ends_in_the_safe_state_without_enabling():
    def press_ctrl_c(servo, n):
        if n == 2:  # the first temperature read while cooling (read 1 was the preflight)
            os.kill(os.getpid(), signal.SIGINT)

    servo = Spy(initial_temperature_degc=50.0, hooks={"read": press_ctrl_c})
    result = run(servo, Hold(0.0, 1.0))
    assert result.outcome is Outcome.INTERRUPTED
    assert result.stop.stage == runner.COOLING
    assert "enable" not in servo.calls
    assert_ends_safe(servo, result)


# == The other monitor checks, through the runner ==================================


def test_a_frozen_position_reading_after_a_step_stops_the_run():
    servo = Spy("stuck_position")
    result = run(servo, Step(0.0, (10 * DEG,), 2.0, 0.5))
    assert result.outcome is Outcome.ABORTED
    assert result.stop.kind == checks.NOT_FOLLOWING
    assert result.trace.segments[result.stop.segment].label == "step 1: 0 deg to 10 deg"
    assert_ends_safe(servo, result)


def test_a_frozen_position_reading_during_a_sine_stops_the_run():
    servo = Spy("stuck_position")
    result = run(servo, SteppedSine(0.0, 10 * DEG, (1.0,), 4, 1, 0.5))
    assert result.outcome is Outcome.ABORTED
    assert result.stop.kind == checks.FROZEN_READING
    assert_ends_safe(servo, result)


def test_a_servo_turning_the_wrong_way_stops_the_run():
    servo = Spy("reversed_direction")
    result = run(servo)
    assert result.outcome is Outcome.ABORTED
    assert result.stop.kind == checks.NOT_FOLLOWING
    assert_ends_safe(servo, result)


def test_a_device_fault_during_the_run_stops_it():
    # temperature_max above the simulated servo's own 70 degC cutoff, so the
    # device's protection acts first and latches a fault.
    servo = Spy("overheating")
    result = run(servo, Hold(0.0, 25.0), bench(temperature_max='"80 degC"'))
    assert result.outcome is Outcome.ABORTED
    assert result.stop.kind == checks.DEVICE_FAULT
    assert "overheat" in result.stop.message
    assert [fault.code for fault in result.faults_after] == ["overheat"]
    assert_ends_safe(servo, result)


def test_a_run_past_experiment_max_is_stopped():
    def stall(servo, n):
        if n == 200:  # one read hangs for 40 s of simulated time
            servo.clock.wait_s(40.0)

    servo = Spy(hooks={"read": stall})
    result = run(servo, Hold(0.0, 20.0))
    assert result.outcome is Outcome.ABORTED
    assert result.stop.kind == checks.DURATION
    assert result.stop.elapsed_s > 30.0
    assert_ends_safe(servo, result)


def test_a_communication_error_on_a_command_is_an_error_and_ends_safe():
    servo = Spy(hooks={"command": on_call(2, CommunicationError("port unplugged"))})
    result = run(servo)
    assert result.outcome is Outcome.ERROR
    assert result.stop.kind == runner.DEVICE_ERROR
    assert "port unplugged" in result.stop.message
    assert_ends_safe(servo, result)


# == Preflight ===================================================================


@pytest.mark.parametrize(
    ("config", "kind"),
    [
        ({"initial_temperature_degc": 75.0}, checks.DEVICE_FAULT),  # latched overheat
        ({"initial_temperature_degc": 65.0}, checks.TEMPERATURE_LIMIT),
        ({"supply_voltage_v": 5.0}, checks.VOLTAGE_LIMIT),
        ({"initial_position_rad": 85 * DEG}, checks.POSITION_LIMIT),
    ],
)
def test_preflight_refuses_without_enabling(config, kind):
    servo = Spy(**config)
    result = run(servo, Hold(0.0, 1.0))
    assert result.outcome is Outcome.REFUSED
    assert result.stop.kind == kind
    assert result.stop.stage == runner.PREFLIGHT
    assert result.rehearsal is None
    assert "enable" not in servo.calls and "command" not in servo.calls
    assert_ends_safe(servo, result)


def test_an_experiment_whose_approach_makes_it_too_long_is_refused():
    servo = Spy(initial_position_rad=30 * DEG)  # 1.5 s of approach at 20 deg/s
    result = run(servo, Hold(0.0, 29.0))
    assert result.outcome is Outcome.REFUSED
    assert result.stop.kind == checks.DURATION
    assert "approach" in result.stop.message


def test_an_adapter_without_a_needed_signal_is_refused_before_contact():
    class NoTemperature(Spy):
        def describe(self):
            description = super().describe()
            signals = tuple(s for s in description.signals if s.name != TEMPERATURE)
            return dataclasses.replace(description, signals=signals)

    servo = NoTemperature()
    result = run(servo, Hold(0.0, 1.0))
    assert result.outcome is Outcome.REFUSED
    assert result.stop.kind == runner.ADAPTER_UNSUITABLE
    assert "temperature_degc" in result.stop.message
    assert servo.calls == []


# == Rehearsal ===================================================================


def test_rehearsal_uses_the_model_parameters_it_is_given():
    assert run(Spy(), SMALL_STEP).outcome is Outcome.COMPLETED
    stiff = dataclasses.replace(DEFAULT_PARAMS, kp_n_m_rad=7.2)  # overshoots, moves faster
    servo = Spy()
    result = run(servo, SMALL_STEP, rehearsal_params=stiff)
    assert result.outcome is Outcome.REFUSED
    assert result.stop.kind == runner.REHEARSAL_FAILED
    assert "the model parameters given for the rehearsal" in result.stop.message
    assert result.rehearsal_params == stiff
    assert "enable" not in servo.calls


def test_rehearsal_starts_where_and_as_warm_as_the_target_is():
    servo = Spy("wrong_zero", initial_temperature_degc=40.0)  # reads 20 deg at rest
    result = run(servo)
    assert result.outcome is Outcome.COMPLETED
    first = result.rehearsal.samples[0]
    assert first[POSITION] == pytest.approx(result.preflight[POSITION], abs=0.2 * DEG)
    assert first[POSITION] == pytest.approx(20 * DEG, abs=0.5 * DEG)
    assert first[TEMPERATURE] == pytest.approx(40.0, abs=0.5)
    # Both start with the same approach ramp from 20 deg back to the centre.
    assert result.rehearsal.segments == result.trace.segments
    assert result.trace.segments[0].kind == "approach"


def test_rehearsal_is_healthy_even_when_the_bench_file_injects_faults():
    faulty = parse_bench(
        TEMPLATE.replace('# faults = ["wrong_zero"]', 'faults = ["runaway"]').encode()
    )
    servo = SimServo.from_bench(faulty)
    result = run_experiment(servo, faulty, SMALL_STEP)
    assert result.rehearsal.stop is None
    assert result.outcome is Outcome.ABORTED and result.stop.kind == checks.SPEED_LIMIT


def test_rehearsal_messages_do_not_claim_to_predict_the_real_device():
    result = run(Spy())
    text = " ".join(result.notes)
    assert "not a prediction of what the real actuator will do" in text
    for claim in ("guarantee", "will be safe", "is safe", "proves"):
        assert claim not in text


# == What a completed run records ================================================


def test_a_completed_run_records_samples_commands_and_segments_on_the_adapter_clock():
    servo = Spy(noise=False)  # at rest it reads exactly 0, so there is no approach ramp
    result = run(servo, Step(0.0, (10 * DEG, -10 * DEG), 1.0, 0.5))
    assert result.outcome is Outcome.COMPLETED and result.stop is None
    trace = result.trace
    times = [sample.t_s for sample in trace.samples]
    assert times == sorted(times) and len(set(times)) == len(times)
    assert times[0] < trace.t_enable_s < trace.t_start_s <= times[1]
    assert trace.t_end_s - trace.t_start_s == pytest.approx(4.5, abs=0.02)
    assert trace.t_end_s <= servo.clock.now_s()
    # One command per constant segment, sent at its start, inside the band.
    assert [command.segment for command in trace.commands] == [0, 1, 2, 3, 4]
    assert [command.value / DEG for command in trace.commands] == pytest.approx([0, 10, 0, -10, 0])
    low_rad, high_rad = command_band_rad(TEMPLATE_BENCH.limits)
    assert all(low_rad <= command.value <= high_rad for command in trace.commands)
    for command, start_s in zip(trace.commands, trace.segment_start_s, strict=True):
        assert start_s <= command.t_s < start_s + 0.02
    assert trace.segment_at(trace.commands[1].t_s + 0.1) == 1
    assert trace.enable_position_rad == trace.samples[0][POSITION]
    assert not trace.missed
    assert result.faults_before == () and result.faults_after == ()
    assert result.description == servo.describe()
    assert result.bench_fingerprint == TEMPLATE_BENCH.fingerprint
    assert_ends_safe(servo, result)


def test_a_stepped_sine_commands_every_sample():
    servo = Spy(noise=False)
    result = run(servo, SteppedSine(0.0, 10 * DEG, (1.0, 2.0), 3, 1, 0.5))
    assert result.outcome is Outcome.COMPLETED
    sine_commands = [c for c in result.trace.commands if c.segment >= 1]
    assert len(sine_commands) > 300
    peak_deg = max(abs(command.value) for command in sine_commands) / DEG
    assert 9.9 < peak_deg <= 10.0


def test_the_runner_never_sleeps_or_reads_the_real_clock(monkeypatch):
    def forbidden(*args):
        raise AssertionError("the runner touched the real clock")

    for name in ("sleep", "time", "time_ns", "monotonic", "monotonic_ns", "perf_counter"):
        monkeypatch.setattr(time, name, forbidden)
    result = run(Spy(), SteppedSine(0.0, 10 * DEG, (1.0,), 3, 1, 0.5))
    assert result.outcome is Outcome.COMPLETED
    servo = Spy(initial_temperature_degc=50.0)  # waits to cool, on the adapter's clock
    assert run(servo, Hold(0.0, 1.0)).cooled_s > 0


# == One way in ==================================================================


def test_run_experiment_has_no_way_to_skip_a_stage():
    parameters = inspect.signature(run_experiment).parameters
    assert list(parameters) == ["adapter", "bench", "spec", "rehearsal_params"]


def test_only_the_runner_enables_or_commands_an_adapter():
    """Outside the adapters, no library code calls enable() or command() except the runner.

    ``Adapter.command(name, value)`` takes two arguments and ``enable()`` none;
    ``Description.command(name)``, a lookup, takes one and is not counted.
    """
    package = Path(servobench.__file__).parent
    allowed = {package / "experiments" / "runner.py"}
    offenders = []
    for path in sorted(package.rglob("*.py")):
        if path in allowed or (package / "adapters") in path.parents:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                arguments = len(node.args) + len(node.keywords)
                if (node.func.attr, arguments) in (("enable", 0), ("command", 2)):
                    offenders.append(f"{path.relative_to(package)}:{node.lineno}")
    assert offenders == []
    # The check does find such calls: the runner has them.
    runner_tree = ast.parse((package / "experiments" / "runner.py").read_text(encoding="utf-8"))
    found = {
        node.func.attr
        for node in ast.walk(runner_tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in ("enable", "command")
        and len(node.args) == {"enable": 0, "command": 2}[node.func.attr]
    }
    assert found == {"enable", "command"}
