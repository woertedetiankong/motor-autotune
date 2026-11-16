"""The experiment runner: the one way to run an experiment on an actuator.

``run_experiment(adapter, bench, spec)`` takes every experiment through the same
stages, in this order. There is no argument, flag or other entry point that
skips one.

1. Validate, without contacting the actuator. The spec against the bench
   limits (``ExperimentSpec.check``), and against what the adapter describes:
   a complete description, the position command accepting the whole command
   band, every signal the monitor needs, the sample rate. A problem refuses the
   run.
2. Preflight, reading only. The device's faults and one sample. The run is
   refused if the device reports a fault, a reading is outside the limits, the
   actuator is outside the command band, or the experiment with its approach
   ramp would last longer than ``experiment_max``.
3. Thermal budget. If the run is due inside the cool-down after the previous
   run on this adapter, or the actuator is above ``start_temperature_max``, the
   runner puts it in its safe state and waits, or refuses, as the bench file's
   ``when_too_warm`` says. After waiting it repeats the preflight.
4. Rehearsal. The same plan, approach included, runs on a simulated servo built
   from the bench file's load and the given model parameters, starting from the
   preflight position and temperature, under the same safety monitor. If the
   monitor stops the rehearsal, the real run is refused and the result says
   which limit was left and when. Rehearsal checks the experiment as designed
   against a model of a servo. It is not a prediction of the real actuator, and
   passing it says nothing certain about the real device; the monitor during
   the real run is what watches the real device.
5. Run. One more reading, then enable (which holds the present position), then
   the plan, with the safety monitor (``servobench.experiments.monitor``)
   checking every command before it is sent and every sample, missed reply and
   fault poll after. The first violation stops the run.
6. Safe state. From stage 2 on, every exit enters the safe state: completion,
   a refusal, a safety abort, an exception anywhere (adapter, monitor, the code
   between samples), Ctrl-C. A failed attempt is retried. If the device never
   confirms, the outcome is ``SAFE_STATE_NOT_CONFIRMED``, whatever else
   happened, and the result says the actuator must be treated as live.

Ctrl-C. While ``run_experiment`` runs in the main thread, Ctrl-C does not raise
``KeyboardInterrupt``: it asks the runner to stop, and the runner does so at the
next sample or within ``WAIT_CHUNK_S`` of a wait, then enters the safe state.
An interrupt therefore never lands half-way through a bus transaction or
between an exception and the safe state. A ``KeyboardInterrupt`` raised some
other way is caught and handled the same way. An adapter call that never
returns cannot be interrupted like this; adapters must time out (``NoReply``).

Time. Every wait and every timestamp is on the adapter's clock, so a run on the
simulated servo is on virtual time.

Honest limits. Python cannot stop other code from calling an adapter directly.
This module is the only code in the tool that enables or commands an adapter;
a test checks that. The device's own protections and the power supply remain
the real guarantee.
"""

from __future__ import annotations

import bisect
import math
import signal
import threading
import weakref
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from typing import TypeVar

from servobench.adapters.sim import SimConfig, SimServo
from servobench.clock import Clock
from servobench.contract import (
    POSITION,
    TEMPERATURE,
    VOLTAGE,
    Adapter,
    AdapterError,
    CommunicationError,
    Description,
    Fault,
    Sample,
    check_description,
)
from servobench.experiments import monitor as checks
from servobench.experiments.monitor import MONITORED_SIGNALS, Monitor, Violation
from servobench.experiments.spec import (
    APPROACH,
    ExperimentSpec,
    Segment,
    build_plan,
    command_band_rad,
)
from servobench.limits import WAIT, Bench, Limits, RunnerSettings
from servobench.model import DEFAULT_PARAMS, PendulumLoad, ServoParams
from servobench.units import Dimension, format_quantity

# How often the device's faults are polled during a run.
FAULT_POLL_INTERVAL_S = 0.5
# The longest single wait; the runner checks for Ctrl-C between chunks.
WAIT_CHUNK_S = 0.2
# How often the temperature is read while waiting for the actuator to cool.
COOL_POLL_INTERVAL_S = 1.0
# Entering the safe state: how many attempts, and the wait between them.
SAFE_STATE_ATTEMPTS = 5
SAFE_STATE_RETRY_WAIT_S = 0.1
# The rehearsal servo's random seed, so a rehearsal is repeatable.
REHEARSAL_SEED = 0

# Stages, in order.
VALIDATE = "validate"
PREFLIGHT = "preflight"
COOLING = "cooling"
REHEARSAL = "rehearsal"
RUN = "run"
STAGES = (VALIDATE, PREFLIGHT, COOLING, REHEARSAL, RUN)

# Kinds of stop besides the monitor's violation kinds (servobench.experiments.monitor).
SPEC_INVALID = "spec_invalid"  # the spec breaks the bench limits
ADAPTER_UNSUITABLE = "adapter_unsuitable"  # the adapter cannot run this experiment safely
TOO_WARM = "too_warm"  # above start_temperature_max
COOLDOWN = "cooldown"  # inside the cool-down after the previous run
MOVED = "moved"  # the actuator moved between the preflight and the run
REHEARSAL_FAILED = "rehearsal_failed"  # the rehearsal left the limits
INTERRUPTED_KIND = "interrupted"  # Ctrl-C
DEVICE_ERROR = "device_error"  # the adapter raised an error
EXCEPTION = "exception"  # any other exception: a bug


class Outcome(Enum):
    """How a run ended."""

    COMPLETED = "completed"  # the whole plan ran inside the limits
    REFUSED = "refused"  # stopped before the actuator was enabled
    ABORTED = "aborted"  # the safety monitor stopped the run
    INTERRUPTED = "interrupted"  # Ctrl-C
    ERROR = "error"  # the adapter or the tool raised an error
    # The safe state could not be confirmed. Overrides every other outcome:
    # the actuator may still be driving its output.
    SAFE_STATE_NOT_CONFIRMED = "safe_state_not_confirmed"


class SafeState(Enum):
    CONFIRMED = "confirmed"  # enter_safe_state returned without an error
    NOT_CONFIRMED = "not_confirmed"  # every attempt failed
    NOT_CONTACTED = "not_contacted"  # refused before the actuator was contacted at all


@dataclass(frozen=True)
class SafeStateReport:
    status: SafeState
    attempts: int
    errors: tuple[str, ...] = ()  # one line per failed attempt
    t_s: float | None = None  # when it was confirmed, on the adapter's clock


@dataclass(frozen=True)
class StopReason:
    """Why a run ended before completing its plan."""

    kind: str  # a violation kind from the monitor, or one of the kinds above
    stage: str  # one of STAGES
    message: str
    t_s: float | None = None  # on the clock of the adapter it happened on
    elapsed_s: float | None = None  # since the experiment started, if it had
    segment: int | None = None  # index into the trace's segments
    value: float | None = None  # the offending value, SI, in ``unit``
    limit: float | None = None  # the limit it broke, SI, in ``unit``
    unit: str | None = None
    # For REHEARSAL_FAILED: what stopped the rehearsal (times on its clock).
    rehearsal: StopReason | None = None


@dataclass(frozen=True)
class CommandRecord:
    t_s: float  # when the adapter sent it, on its clock
    name: str
    value: float  # SI
    segment: int


@dataclass(frozen=True)
class MissedRead:
    t_s: float  # when the read was given up, on the adapter's clock
    error: str
    segment: int | None


@dataclass(frozen=True)
class Trace:
    """Everything that happened on one adapter during one run of a plan."""

    simulated: bool
    segments: tuple[Segment, ...]
    # When each segment started, on the adapter's clock; one entry per
    # segment that was reached.
    segment_start_s: tuple[float, ...]
    samples: tuple[Sample, ...]  # the first is the reading just before enable
    commands: tuple[CommandRecord, ...]
    missed: tuple[MissedRead, ...]
    enable_position_rad: float | None  # what enable held
    t_enable_s: float | None  # when enable was called; None if it never was
    t_start_s: float | None  # when the plan started (enable returned)
    t_end_s: float | None  # when the run stopped, before the safe state
    stop: StopReason | None  # None: the plan ran to the end

    def segment_at(self, t_s: float) -> int | None:
        """The index of the segment running at ``t_s``, or None before the plan started."""
        if not self.segment_start_s or t_s < self.segment_start_s[0]:
            return None
        return bisect.bisect_right(self.segment_start_s, t_s) - 1

    def elapsed_s(self, t_s: float) -> float | None:
        return None if self.t_start_s is None else t_s - self.t_start_s


@dataclass(frozen=True)
class RunResult:
    """The result of ``run_experiment``."""

    outcome: Outcome
    stop: StopReason | None  # None only for COMPLETED, or a completed run whose safe state failed
    safe_state: SafeStateReport
    spec: ExperimentSpec
    bench_fingerprint: str
    bench_source: str
    limits: Limits
    runner: RunnerSettings
    description: Description
    rehearsal_params: ServoParams
    preflight: Sample | None  # the reading the rehearsal started from
    faults_before: tuple[Fault, ...] | None  # at preflight
    faults_after: tuple[Fault, ...] | None  # after the safe state; None if unreadable
    cooled_s: float  # time spent waiting for the cool-down or to cool
    rehearsal: Trace | None
    trace: Trace | None  # the real run; None if the actuator was never enabled
    notes: tuple[str, ...] = ()

    @property
    def safe_state_confirmed(self) -> bool:
        return self.safe_state.status is SafeState.CONFIRMED

    @property
    def summary(self) -> str:
        return _summary(self)


def run_experiment(
    adapter: Adapter,
    bench: Bench,
    spec: ExperimentSpec,
    *,
    rehearsal_params: ServoParams = DEFAULT_PARAMS,
) -> RunResult:
    """Run ``spec`` on ``adapter`` within ``bench``'s limits. The stages are in the module text.

    ``rehearsal_params`` are the servo model's parameters for the rehearsal:
    the default placeholders, or a fitted set. Whatever happens on the device
    or in the run, the result says so; nothing is raised. The only exceptions
    that escape are from a broken spec or a broken ``describe()``, before the
    actuator is contacted, and ``SystemExit`` and the like, after the safe state.
    """
    run = _Run(adapter, bench, spec, rehearsal_params)
    with _ctrl_c_requests_stop() as stop_request:
        problems = run.validate()
        if problems:
            return run.result(
                StopReason(problems[0][0], VALIDATE, "; ".join(text for _, text in problems)),
                SafeStateReport(SafeState.NOT_CONTACTED, 0),
            )
        stop: StopReason | None = None
        try:
            run.stages(stop_request)
        except _Stopped as stopped:
            stop = stopped.reason
        except KeyboardInterrupt:
            stop = run.stop_here(INTERRUPTED_KIND, "interrupted by Ctrl-C (KeyboardInterrupt)")
        except AdapterError as error:
            stop = run.stop_here(DEVICE_ERROR, f"{type(error).__name__}: {error}")
        except Exception as error:
            stop = run.stop_here(EXCEPTION, f"{type(error).__name__}: {error}")
        finally:
            # Nothing else may come first: whatever ended the run, the safe state.
            safe = _enter_safe_state(adapter)
            try:
                run.finish()
            except Exception as error:  # noqa: BLE001 - bookkeeping after the safe state
                run.notes.append(f"after the run: {type(error).__name__}: {error}")
        if stop is None and run.target_trace is not None:
            stop = run.target_trace.stop
        return run.result(stop, safe)


# -- the stages ------------------------------------------------------------------------


class _Stopped(Exception):
    """Unwinds the stages with the reason a run stopped."""

    def __init__(self, reason: StopReason) -> None:
        super().__init__(reason.message)
        self.reason = reason


class _StopRequest:
    requested = False


class _RunEnds:
    """When the last run on each adapter object ended, on its clock, for the cool-down.

    Kept here, not passed in, so no caller can leave it out. Keyed by the
    adapter object itself; a new adapter object for the same device starts with
    no record, and so do runs in another process: there the start temperature
    check still applies.
    """

    def __init__(self) -> None:
        self._ends: dict[int, tuple[Callable[[], object], float]] = {}

    def get(self, adapter: Adapter) -> float | None:
        entry = self._ends.get(id(adapter))
        if entry is None or entry[0]() is not adapter:
            return None
        return entry[1]

    def set(self, adapter: Adapter, end_s: float) -> None:
        try:
            ref: Callable[[], object] = weakref.ref(adapter)
        except TypeError:  # an object that cannot be weakly referenced: keep it
            ref = lambda kept=adapter: kept  # noqa: E731
        self._ends[id(adapter)] = (ref, end_s)


_last_run_end_s = _RunEnds()

T = TypeVar("T")


class _Run:
    def __init__(
        self, adapter: Adapter, bench: Bench, spec: ExperimentSpec, params: ServoParams
    ) -> None:
        self.adapter = adapter
        self.bench = bench
        self.limits = bench.limits
        self.settings = bench.runner
        self.spec = spec
        self.params = params
        self.description = adapter.describe()
        self.clock: Clock = adapter.clock
        self.stage = VALIDATE
        self.preflight: Sample | None = None
        self.faults_before: tuple[Fault, ...] | None = None
        self.faults_after: tuple[Fault, ...] | None = None
        self.cooled_s = 0.0
        self.cool_deadline_s: float | None = None
        self.rehearsal_trace: Trace | None = None
        self.target: _TraceBuilder | None = None
        self.target_trace: Trace | None = None
        self.notes: list[str] = []
        rate_hz = self.description.max_sample_rate_hz
        if spec.sample_rate_hz is not None:
            rate_hz = min(rate_hz, spec.sample_rate_hz)
        self.period_s = 1.0 / rate_hz

    # Stage 1 ------------------------------------------------------------------------

    def validate(self) -> list[tuple[str, str]]:
        problems = [(SPEC_INVALID, text) for text in self.spec.check(self.limits)]
        description = self.description
        unsuitable = [f"its description is incomplete: {p}" for p in check_description(description)]
        if POSITION not in {command.name for command in description.commands}:
            unsuitable.append(f"it cannot be commanded to a {POSITION}")
        else:
            info = description.command(POSITION)
            band_low_rad, band_high_rad = command_band_rad(self.limits)
            if band_low_rad < info.minimum or band_high_rad > info.maximum:
                unsuitable.append(
                    f"it accepts {POSITION} commands only from {_deg(info.minimum)} to"
                    f" {_deg(info.maximum)}, narrower than the bench file's command range"
                    f" {_deg(band_low_rad)} to {_deg(band_high_rad)}"
                )
        missing = [name for name in MONITORED_SIGNALS if name not in description.signal_names]
        if missing:
            unsuitable.append(
                f"it cannot read {', '.join(missing)}, which the safety monitor needs to"
                " enforce the bench limits"
            )
        if (
            self.spec.sample_rate_hz is not None
            and self.spec.sample_rate_hz > description.max_sample_rate_hz
        ):
            unsuitable.append(
                f"the spec asks for {self.spec.sample_rate_hz} Hz, above the adapter's"
                f" reliable {description.max_sample_rate_hz} Hz"
            )
        problems += [
            (ADAPTER_UNSUITABLE, f"adapter {description.identity.adapter!r}: {text}")
            for text in unsuitable
        ]
        return problems

    # Stages 2 to 5 ----------------------------------------------------------------

    def stages(self, stop_request: _StopRequest) -> None:
        self.stop_request = stop_request
        self.stage = PREFLIGHT
        plan = self.run_preflight()
        self.stage = COOLING
        while self.cool_down():
            # It waited: look again, and wait again if a fresh reading is still too warm.
            self.stage = PREFLIGHT
            plan = self.run_preflight()
            self.stage = COOLING
        self.check_interrupt()

        self.stage = REHEARSAL
        self.rehearse(plan)
        self.check_interrupt()

        self.stage = RUN
        self.target = _TraceBuilder(simulated=self.description.identity.simulated, plan=plan)
        try:
            _execute(
                self.adapter,
                plan,
                Monitor(self.limits, self.settings),
                self.target,
                self.period_s,
                stop_request,
            )
        finally:
            self.target_trace = self.target.freeze()

    def run_preflight(self) -> tuple[Segment, ...]:
        limits = self.limits
        faults = self.read_retrying(self.adapter.read_faults)
        self.faults_before = faults
        if faults:
            listed = "; ".join(f"{fault.code}: {fault.description}" for fault in faults)
            self.refuse(
                checks.DEVICE_FAULT,
                f"the device reports a fault ({listed}). The tool cannot clear faults; a human"
                " must find the cause and clear it",
            )
        sample = self.read_retrying(self.adapter.read)
        self.preflight = sample
        monitor = Monitor(limits, self.settings)
        violation = monitor.on_sample(sample)
        if violation is not None:
            self.refuse(violation.kind, f"before starting: {violation.message}", violation)
        position_rad = sample[POSITION]
        band_low_rad, band_high_rad = command_band_rad(limits)
        if not band_low_rad <= position_rad <= band_high_rad:
            self.refuse(
                checks.POSITION_LIMIT,
                f"the actuator is at {_deg(position_rad)}, outside the command range"
                f" {_deg(band_low_rad)} to {_deg(band_high_rad)}. With the power off, move it"
                " by hand into that range",
            )
        plan = build_plan(self.spec, position_rad, self.settings.approach_speed_rad_s)
        total_s = math.fsum(segment.duration_s for segment in plan)
        if total_s > limits.experiment_max_s:
            self.refuse(
                checks.DURATION,
                f"the experiment with its approach from {_deg(position_rad)} would last"
                f" {_s(total_s)}, longer than experiment_max {_s(limits.experiment_max_s)}",
            )
        return plan

    def cool_down(self) -> bool:
        """Wait for the cool-down and the temperature, or refuse. Whether it waited."""
        limits, settings = self.limits, self.settings
        last_end_s = _last_run_end_s.get(self.adapter)
        now_s = self.clock.now_s()
        rest_left_s = 0.0 if last_end_s is None else last_end_s + limits.cooldown_s - now_s
        temperature_degc = self.preflight[TEMPERATURE]
        too_warm = temperature_degc > limits.start_temperature_max_degc
        if rest_left_s <= 0 and not too_warm:
            return False
        if settings.when_too_warm != WAIT:
            if rest_left_s > 0:
                self.refuse(
                    COOLDOWN,
                    f"the previous run on this actuator ended {_s(now_s - last_end_s)} ago;"
                    f" the cool-down is {_s(limits.cooldown_s)}. Try again in {_s(rest_left_s)}",
                )
            self.refuse(TOO_WARM, self.too_warm_message(temperature_degc))

        # Wait at rest, in the safe state, so the actuator cools. The cool-down
        # after a run is waited in full; waiting for the temperature to fall is
        # limited to cool_wait_max in all.
        self.adapter.enter_safe_state()
        start_s = self.clock.now_s()
        try:
            if rest_left_s > 0:
                self.wait(rest_left_s)
            if self.cool_deadline_s is None:
                self.cool_deadline_s = self.clock.now_s() + settings.cool_wait_max_s
            while True:
                sample = self.read_retrying(lambda: self.adapter.read([TEMPERATURE]))
                temperature_degc = sample[TEMPERATURE]
                if temperature_degc <= limits.start_temperature_max_degc:
                    return True
                if self.clock.now_s() >= self.cool_deadline_s:
                    self.refuse(
                        TOO_WARM,
                        self.too_warm_message(temperature_degc)
                        + " and did not cool below it within cool_wait_max"
                        f" {_s(settings.cool_wait_max_s)}",
                    )
                self.wait(COOL_POLL_INTERVAL_S)
        finally:
            waited_s = self.clock.now_s() - start_s
            self.cooled_s += waited_s
            self.notes.append(f"waited {_s(waited_s)} at rest in the safe state to cool down")

    def too_warm_message(self, temperature_degc: float) -> str:
        return (
            f"the actuator is at {_degc(temperature_degc)}, above start_temperature_max"
            f" {_degc(self.limits.start_temperature_max_degc)}"
        )

    def rehearse(self, plan: tuple[Segment, ...]) -> None:
        servo = _rehearsal_servo(self.bench, self.params, self.preflight, self.description)
        builder = _TraceBuilder(simulated=True, plan=plan)
        try:
            _execute(
                servo,
                plan,
                Monitor(self.limits, self.settings),
                builder,
                self.period_s,
                self.stop_request,
            )
        finally:
            servo.enter_safe_state()
            self.rehearsal_trace = builder.freeze()
        stop = self.rehearsal_trace.stop
        if stop is None:
            self.notes.append(
                f"rehearsal: the plan ran on the simulated servo ({_params_note(self.params)})"
                " and stayed inside the limits. This checks the experiment as designed against"
                " the model; it is not a prediction of what the real actuator will do"
            )
            return
        if stop.kind == INTERRUPTED_KIND:
            raise _Stopped(self.stop_here(INTERRUPTED_KIND, "interrupted by Ctrl-C"))
        when = "" if stop.elapsed_s is None else f" {_s(stop.elapsed_s)} into the experiment"
        where = "" if stop.segment is None else f", during '{plan[stop.segment].label}'"
        raise _Stopped(
            StopReason(
                REHEARSAL_FAILED,
                REHEARSAL,
                f"the rehearsal on the simulated servo ({_params_note(self.params)}) left the"
                f" limits{when}{where}: {stop.message}. The real actuator was not enabled."
                " Change the experiment so it stays inside the limits; do not loosen them",
                rehearsal=stop,
            )
        )

    # Helpers -------------------------------------------------------------------------

    def read_retrying(self, call: Callable[[], T]) -> T:
        """Call a read, allowing missed_replies_max missed replies in a row."""
        monitor = Monitor(self.limits, self.settings)
        while True:
            self.check_interrupt()
            try:
                return call()
            except CommunicationError as error:
                violation = monitor.on_missed(
                    self.clock.now_s(), f"{type(error).__name__}: {error}"
                )
                if violation is not None:
                    raise _Stopped(
                        StopReason(
                            violation.kind,
                            self.stage,
                            violation.message,
                            violation.t_s,
                            value=violation.value,
                            limit=violation.limit,
                            unit=violation.unit,
                        )
                    ) from None

    def wait(self, duration_s: float) -> None:
        end_s = self.clock.now_s() + duration_s
        while self.clock.now_s() < end_s:
            self.check_interrupt()
            self.clock.wait_until_s(min(end_s, self.clock.now_s() + WAIT_CHUNK_S))

    def check_interrupt(self) -> None:
        if self.stop_request.requested:
            raise _Stopped(self.stop_here(INTERRUPTED_KIND, "interrupted by Ctrl-C"))

    def refuse(self, kind: str, message: str, violation: Violation | None = None) -> None:
        raise _Stopped(
            StopReason(
                kind,
                self.stage,
                message,
                _now(self.clock),
                value=None if violation is None else violation.value,
                limit=None if violation is None else violation.limit,
                unit=None if violation is None else violation.unit,
            )
        )

    def stop_here(self, kind: str, message: str) -> StopReason:
        """A stop at the present moment: during the run if it had started, else in its stage."""
        builder = self.target
        t_s = _now(self.clock)
        if builder is not None and builder.t_start_s is not None and t_s is not None:
            return StopReason(
                kind, RUN, message, t_s, t_s - builder.t_start_s, builder.segment_at(t_s)
            )
        return StopReason(kind, self.stage, message, t_s)

    def finish(self) -> None:
        """After the safe state: note the end for the cool-down, read the faults."""
        if self.target is not None and self.target.t_enable_s is not None:
            end_s = _now(self.clock)
            if end_s is not None:
                _last_run_end_s.set(self.adapter, end_s)
        try:
            self.faults_after = self.adapter.read_faults()
        except BaseException as error:  # noqa: BLE001 - after the safe state, only recorded
            self.notes.append(f"the faults could not be read after the run: {error!r}")

    def result(self, stop: StopReason | None, safe: SafeStateReport) -> RunResult:
        return RunResult(
            outcome=_outcome(stop, safe),
            stop=stop,
            safe_state=safe,
            spec=self.spec,
            bench_fingerprint=self.bench.fingerprint,
            bench_source=self.bench.source,
            limits=self.limits,
            runner=self.settings,
            description=self.description,
            rehearsal_params=self.params,
            preflight=self.preflight,
            faults_before=self.faults_before,
            faults_after=self.faults_after,
            cooled_s=self.cooled_s,
            rehearsal=self.rehearsal_trace,
            trace=self.target_trace,
            notes=tuple(self.notes),
        )


# -- running a plan on one adapter ----------------------------------------------------


@dataclass
class _TraceBuilder:
    simulated: bool
    plan: tuple[Segment, ...]
    segment_start_s: list[float] = field(default_factory=list)
    samples: list[Sample] = field(default_factory=list)
    commands: list[CommandRecord] = field(default_factory=list)
    missed: list[MissedRead] = field(default_factory=list)
    enable_position_rad: float | None = None
    t_enable_s: float | None = None
    t_start_s: float | None = None
    t_end_s: float | None = None
    stop: StopReason | None = None

    def segment_at(self, t_s: float) -> int | None:
        starts = self.segment_start_s
        if not starts or t_s < starts[0]:
            return None
        return bisect.bisect_right(starts, t_s) - 1

    def stopped(self, violation: Violation, stage: str = RUN) -> None:
        self.stop = StopReason(
            violation.kind,
            stage,
            violation.message,
            violation.t_s,
            None if self.t_start_s is None else violation.t_s - self.t_start_s,
            self.segment_at(violation.t_s),
            violation.value,
            violation.limit,
            violation.unit,
        )

    def freeze(self) -> Trace:
        return Trace(
            simulated=self.simulated,
            segments=self.plan,
            segment_start_s=tuple(self.segment_start_s),
            samples=tuple(self.samples),
            commands=tuple(self.commands),
            missed=tuple(self.missed),
            enable_position_rad=self.enable_position_rad,
            t_enable_s=self.t_enable_s,
            t_start_s=self.t_start_s,
            t_end_s=self.t_end_s,
            stop=self.stop,
        )


def _execute(
    adapter: Adapter,
    plan: tuple[Segment, ...],
    monitor: Monitor,
    trace: _TraceBuilder,
    period_s: float,
    stop_request: _StopRequest,
) -> None:
    """Enable ``adapter`` and run ``plan`` under ``monitor``, recording into ``trace``.

    Returns when the plan is done or the monitor stops it (``trace.stop``).
    Exceptions propagate, with ``trace`` holding what happened up to then. The
    caller enters the safe state.
    """
    clock = adapter.clock
    try:
        # A reading just before enable: checked, recorded, and the position that
        # enable will hold.
        while True:
            try:
                sample = adapter.read()
            except CommunicationError as error:
                if _missed(trace, monitor, clock, error, None):
                    return
                continue
            trace.samples.append(sample)
            violation = monitor.on_sample(sample)
            if violation is not None:
                trace.stopped(violation, PREFLIGHT)
                return
            break
        hold_rad = sample[POSITION]
        first = plan[0]
        start_rad = first.from_position_rad if first.kind == APPROACH else first.position_rad
        follow_rad = monitor.limits.follow_error_max_rad
        if abs(hold_rad - start_rad) > follow_rad:
            trace.stop = StopReason(
                MOVED,
                PREFLIGHT,
                f"the actuator moved from {_deg(start_rad)} to {_deg(hold_rad)} after the"
                " rehearsal was planned, more than follow_error_max; it was not enabled",
                sample.t_s,
            )
            return
        if stop_request.requested:
            trace.stop = StopReason(
                INTERRUPTED_KIND, PREFLIGHT, "interrupted by Ctrl-C", sample.t_s
            )
            return

        trace.enable_position_rad = hold_rad
        monitor.hold(clock.now_s(), hold_rad)
        trace.t_enable_s = clock.now_s()  # set first: a failed enable may still have acted
        adapter.enable()
        t0_s = clock.now_s()
        trace.t_start_s = t0_s
        monitor.start(t0_s)
        starts_s: list[float] = []
        t_s = t0_s
        for segment in plan:
            starts_s.append(t_s)
            t_s += segment.duration_s
        end_s = t_s
        next_poll_s = t0_s + FAULT_POLL_INTERVAL_S
        sent_segment = -1

        while True:
            now_s = clock.now_s()
            if stop_request.requested:
                trace.stop = StopReason(
                    INTERRUPTED_KIND,
                    RUN,
                    "interrupted by Ctrl-C",
                    now_s,
                    now_s - t0_s,
                    trace.segment_at(now_s),
                )
                return
            # The duration first: a run that a stalled call carried past the
            # end of its plan has still been enabled too long.
            violation = monitor.check_time(now_s)
            if violation is not None:
                trace.stopped(violation)
                return
            if now_s >= end_s:
                return

            index = bisect.bisect_right(starts_s, now_s) - 1
            while len(trace.segment_start_s) <= index:
                trace.segment_start_s.append(starts_s[len(trace.segment_start_s)])
            segment = plan[index]
            if index != sent_segment or not segment.constant:
                value_rad = segment.setpoint_rad(now_s - starts_s[index])
                violation = monitor.check_command(now_s, value_rad)
                if violation is not None:
                    trace.stopped(violation)
                    return
                sent_s = adapter.command(POSITION, value_rad)
                trace.commands.append(CommandRecord(sent_s, POSITION, value_rad, index))
                monitor.on_command(sent_s, value_rad)
                sent_segment = index

            try:
                sample = adapter.read()
            except CommunicationError as error:
                if _missed(trace, monitor, clock, error, index):
                    return
            else:
                trace.samples.append(sample)
                violation = monitor.on_sample(sample)
                if violation is not None:
                    trace.stopped(violation)
                    return

            if clock.now_s() >= next_poll_s:
                next_poll_s = clock.now_s() + FAULT_POLL_INTERVAL_S
                try:
                    faults = adapter.read_faults()
                except CommunicationError as error:
                    if _missed(trace, monitor, clock, error, index):
                        return
                else:
                    violation = monitor.on_faults(clock.now_s(), faults)
                    if violation is not None:
                        trace.stopped(violation)
                        return

            clock.wait_until_s(now_s + period_s)
    finally:
        trace.t_end_s = _now(clock)


def _missed(
    trace: _TraceBuilder, monitor: Monitor, clock: Clock, error: Exception, segment: int | None
) -> bool:
    """Record a missed reply; whether it stops the run."""
    t_s = clock.now_s()
    text = f"{type(error).__name__}: {error}"
    trace.missed.append(MissedRead(t_s, text, segment))
    violation = monitor.on_missed(t_s, text)
    if violation is None:
        return False
    trace.stopped(violation, RUN if trace.t_start_s is not None else PREFLIGHT)
    return True


def _rehearsal_servo(
    bench: Bench, params: ServoParams, start: Sample, description: Description
) -> SimServo:
    """A healthy simulated servo, starting where and as warm as the real one is now.

    The bench file's [sim] settings and faults are not used: they describe the
    simulated target, not the experiment being rehearsed.
    """
    limits = bench.limits
    reach_rad = max(math.pi, abs(limits.position_min_rad), abs(limits.position_max_rad))
    config = SimConfig(
        seed=REHEARSAL_SEED,
        sample_rate_hz=description.max_sample_rate_hz,
        supply_voltage_v=start[VOLTAGE],
        initial_temperature_degc=start[TEMPERATURE],
        initial_position_rad=start[POSITION],
        command_limit_rad=reach_rad,
    )
    return SimServo(config, params=params, load=PendulumLoad.from_bench(bench.load))


# -- the safe state ------------------------------------------------------------------


def _enter_safe_state(adapter: Adapter) -> SafeStateReport:
    """Enter the safe state, retrying. Never raises; the report says what happened."""
    errors: list[str] = []
    for attempt in range(1, SAFE_STATE_ATTEMPTS + 1):
        try:
            adapter.enter_safe_state()
        except BaseException as error:  # noqa: BLE001 - Ctrl-C included: keep trying
            errors.append(f"attempt {attempt}: {type(error).__name__}: {error}")
            if attempt < SAFE_STATE_ATTEMPTS:
                try:
                    adapter.clock.wait_s(SAFE_STATE_RETRY_WAIT_S)
                except BaseException as wait_error:  # noqa: BLE001
                    errors.append(f"waiting to retry: {type(wait_error).__name__}: {wait_error}")
            continue
        try:
            t_s = adapter.clock.now_s()
        except BaseException:  # noqa: BLE001
            t_s = None
        return SafeStateReport(SafeState.CONFIRMED, attempt, tuple(errors), t_s)
    return SafeStateReport(SafeState.NOT_CONFIRMED, SAFE_STATE_ATTEMPTS, tuple(errors))


@contextmanager
def _ctrl_c_requests_stop() -> Iterator[_StopRequest]:
    """While inside, Ctrl-C sets a flag instead of raising (main thread only)."""
    request = _StopRequest()
    if threading.current_thread() is not threading.main_thread():
        yield request  # Ctrl-C is delivered only to the main thread
        return

    def handler(signum, frame) -> None:
        request.requested = True

    previous = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, handler)
    try:
        yield request
    finally:
        signal.signal(signal.SIGINT, previous if previous is not None else signal.SIG_DFL)


# -- outcome and summary --------------------------------------------------------------


def _outcome(stop: StopReason | None, safe: SafeStateReport) -> Outcome:
    if safe.status is SafeState.NOT_CONFIRMED:
        return Outcome.SAFE_STATE_NOT_CONFIRMED
    if stop is None:
        return Outcome.COMPLETED
    if stop.kind == INTERRUPTED_KIND:
        return Outcome.INTERRUPTED
    if stop.kind in (DEVICE_ERROR, EXCEPTION):
        return Outcome.ERROR
    if stop.stage == RUN:
        return Outcome.ABORTED
    return Outcome.REFUSED


def _summary(result: RunResult) -> str:
    stop = result.stop
    kind = result.spec.kind
    where = ""
    if stop is not None and stop.elapsed_s is not None:
        where = f" {_s(stop.elapsed_s)} into the experiment"
        if stop.segment is not None and result.trace is not None:
            where += f", during '{result.trace.segments[stop.segment].label}'"
    reason = "" if stop is None else stop.message
    if result.outcome is Outcome.SAFE_STATE_NOT_CONFIRMED:
        ended = "it ran to the end" if stop is None else f"{stop.kind}{where}: {reason}"
        return (
            "SAFE STATE NOT CONFIRMED. The runner tried"
            f" {result.safe_state.attempts} times to put the actuator in its safe state"
            f" ({result.description.safe_state}) and the device never confirmed it. Treat the"
            " actuator as live and still driving: cut its power now, and run nothing until a"
            f" human has checked it. The run itself ended because {ended}"
        )
    safe = "The actuator is in its safe state."
    if result.outcome is Outcome.COMPLETED:
        return f"Completed: the {kind} experiment ran to the end inside the limits. {safe}"
    if result.outcome is Outcome.ABORTED:
        return f"Safety abort{where}: {reason}. {safe}"
    if result.outcome is Outcome.INTERRUPTED:
        return f"Interrupted by Ctrl-C{where}. {safe}"
    if result.outcome is Outcome.ERROR:
        return f"Error during {stop.stage}{where}: {reason}. {safe}"
    if result.safe_state.status is SafeState.NOT_CONTACTED:
        return f"Refused before contacting the actuator: {reason}."
    return f"Refused before the actuator was enabled ({stop.stage}): {reason}. {safe}"


def _params_note(params: ServoParams) -> str:
    if params == DEFAULT_PARAMS:
        return "default placeholder model parameters, not measured on any real servo"
    return "the model parameters given for the rehearsal"


def _now(clock: Clock) -> float | None:
    try:
        return clock.now_s()
    except Exception:  # noqa: BLE001 - only for reports
        return None


def _deg(value_rad: float) -> str:
    return format_quantity(value_rad, Dimension.ANGLE, "deg", digits=4)


def _s(value_s: float) -> str:
    return format_quantity(value_s, Dimension.TIME, "s", digits=4)


def _degc(value_degc: float) -> str:
    return format_quantity(value_degc, Dimension.TEMPERATURE, digits=4)
