"""The characterize command: how does this actuator actually behave?

``run_characterize(bench)`` measures the servo model's parameters
(``servobench.model``) on the bench file's actuator, says how well each is
known, checks the fitted model on an experiment it was not fitted on, and
exports the result for a simulator. It returns a ``CommandResult``.

The sequence:

1. **Gate.** ``characterize`` needs a ``[load]`` in the bench file: the known
   gravity torque of the load is what separates the servo's inertia from its
   gains. Without one it refuses before contacting anything (exit 2). Then it
   runs ``check`` in full, motion included, on the same adapter. A reversed or
   wrongly zeroed servo produces ordinary-looking data that the fit would
   turn into confident nonsense, so ``characterize`` moves the actuator for
   itself only if ``check``'s verdict is ``pass``. Otherwise it returns
   ``check``'s verdict, exit code, ``needs_human`` and ``next`` (a ``partial``
   check, something not established, becomes ``refused`` with exit 3), and the
   summary says that nothing was characterized.
2. **The experiments** (``plan_experiments``), each through ``run_experiment``
   and so through validation, rehearsal, the safety monitor and the safe state,
   each with its own run record under ``runs/``: a set of steps at several
   amplitudes, a stepped sine and one slow, large sine through the load's
   gravity torque, which are fitted, and a second set of steps at other
   amplitudes, held out of the fit. All are centred at zero, where the arm
   hangs, and sized from the bench limits. Before each experiment it waits until
   ``cooldown`` has passed since the previous one ended, on the adapter's clock
   (the runner holds the same rest, and refuses or waits for temperature as the
   bench file says). If a run does not complete (refused, aborted, an error,
   the safe state not confirmed), the sequence stops there: nothing more is
   run, nothing is fitted, and the verdict, exit code and ``needs_human`` are
   ``judge_run``'s, as for ``check``.
3. **The fit** (``servobench.analysis.fit``) on the fitted experiments, and the
   held-out experiment replayed with the default placeholder parameters and
   with the fitted ones.
4. **The verdict.** The fit describes the actuator ("good") when the fitted
   model's RMS error on the held-out experiment is at most
   ``GOOD_FIT_NOISE_RATIO`` times the readings' noise floor. Otherwise it is
   ``fail`` (exit 1, ``needs_human``): the model does not describe this
   actuator well, the parameters are reported but marked as not to be used,
   and no MuJoCo fragment is written. A good fit with any parameter not
   identifiable is ``partial`` (exit 0): under the template's limits the servo
   never saturates, so the torque limit and the split of the damping between
   the derivative gain and viscous friction are always among them. ``pass``
   needs every parameter identified.

The files: the per-experiment run records (``parts``), and one record of the
command itself holding ``servo_model.json`` and ``servo_model_mujoco.xml``
(``servobench.export``) and ``holdout_overlay.png``, the measured, default and
fitted responses on the held-out experiment.
"""

from __future__ import annotations

import dataclasses
import json
import math
import statistics
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from servobench import __version__, export, records
from servobench.adapters import UnknownAdapter, open_adapter
from servobench.analysis import analyse, as_metrics
from servobench.analysis.fit import (
    DAMPING_TOTAL,
    PARAMETERS,
    FitExperiment,
    FitResult,
    fit_model,
    noise_floor,
    prediction_error,
    replay,
)
from servobench.analysis.frequency import FrequencyAnalysis
from servobench.analysis.step import StepAnalysis
from servobench.clock import utc_now
from servobench.commands import (
    ERROR,
    FAIL,
    PARTIAL,
    PASS,
    REFUSED,
    CommandResult,
    ExitCode,
    error_details,
    judge_run,
)
from servobench.commands.check import run_check
from servobench.contract import TEMPERATURE, VOLTAGE, Adapter, AdapterError, Description
from servobench.experiments.runner import RunResult, run_experiment
from servobench.experiments.spec import ExperimentSpec, Step, SteppedSine, command_band_rad
from servobench.limits import Bench
from servobench.model import DEFAULT_PARAMS, PendulumLoad, ServoParams, simulate
from servobench.plots import plot_overlay
from servobench.units import Dimension, format_quantity

COMMAND = "characterize"

# The fit describes the actuator when the fitted model's RMS error on the
# held-out experiment is at most this many times the noise floor
# (servobench.analysis.fit.NoiseFloor), the error a model that described the
# actuator exactly would still have. Measured on the simulated servo (plan
# step 8, 60 runs over three servos): a servo the model describes exactly
# scores 0.89 to 1.12 with noise and jitter, 0.64 to 0.76 without noise. Backlash, which
# the model lacks, scored 1.35 to 1.39 at 0.3 deg (not caught: 3.4 reading
# steps of play), 1.64 to 1.70 at 0.5 deg and 1.69 to 1.76 at 0.6 deg; 0.75
# deg or more already fails check's direction item. The
# fit absorbs backlash partly as extra Coulomb friction while still reporting
# it with a small uncertainty, so this judgement is what stops such numbers
# being used.
GOOD_FIT_NOISE_RATIO = 1.5

# The experiment set (plan_experiments).
# The largest step: its peak speed, predicted by the model the rehearsal uses,
# at most this share of speed_max; its overshoot at most this share of
# follow_error_max. The margin is for a real servo faster than the model.
STEP_SPEED_SHARE = 0.8
STEP_OVERSHOOT_SHARE = 0.5
# The fitted steps, as shares of the largest; each up and back, held DWELL_S.
FIT_STEP_SHARES = (1.0, -1.0, 0.5, -0.5, 0.25, -0.25)
# The held-out steps: other amplitudes, inside the fitted range, so that the
# comparison is of the model, not of an extrapolation.
HOLDOUT_STEP_SHARES = (0.75, -0.75, 0.35, -0.35)
DWELL_S = 1.0
DWELL_MIN_S = 0.5
SETTLE_S = 0.5
# The sine: five frequencies, log-spaced over a factor of eight up to the
# highest, which has at least SINE_SAMPLES_PER_CYCLE readings per cycle and is
# at most SINE_HZ_MAX. The amplitude keeps the commanded speed at the highest
# frequency to SINE_COMMAND_SPEED_SHARE of speed_max, and the predicted
# response speed to STEP_SPEED_SHARE.
SINE_HZ_MAX = 8.0
SINE_SAMPLES_PER_CYCLE = 16
SINE_FREQUENCIES = 5
SINE_SPAN = 8.0
SINE_COMMAND_SPEED_SHARE = 0.5
SINE_CYCLES = 4
SINE_TRANSIENT_CYCLES = 1
# The sweep: one slow cycle of a large sine, for the gravity torque. Its
# amplitude is half the command range on the narrower side of zero, at most
# SWEEP_AMPLITUDE_MAX; its frequency at most SWEEP_HZ_MAX, and lower if the
# commanded speed would pass SINE_COMMAND_SPEED_SHARE of speed_max. The known
# gravity torque of the load is what fixes the scale of the gains, the
# inertia and the friction; the steps alone, at under 12 deg under the
# template, sag under it by about three reading steps, which is too little.
SWEEP_ROOM_SHARE = 0.5
SWEEP_AMPLITUDE_MAX_RAD = math.radians(40.0)
SWEEP_HZ_MAX = 0.1
# Every experiment, approach included, fits inside this share of experiment_max.
DURATION_SHARE = 0.9
# Amplitudes are rounded down to this.
AMPLITUDE_ROUNDING_RAD = math.radians(0.1)

FIT = "fit"
HOLDOUT = "holdout"

POOR_FIT_PREFIX = (
    "POOR FIT: determined by the data under a model that does not describe this actuator"
    " (validation.fit_quality is poor); a parameter of that model, not a measurement of the"
    " actuator, and its uncertainty does not include the model's error. "
)


@dataclass(frozen=True)
class PlannedExperiment:
    name: str
    role: str  # FIT or HOLDOUT
    spec: ExperimentSpec
    why: str


@dataclass(frozen=True)
class ExperimentPlan:
    experiments: tuple[PlannedExperiment, ...]
    design: dict[str, Any]  # how each number was derived from the bench limits


class PlanError(Exception):
    """The bench limits leave no room for the experiment set."""


def plan_experiments(
    bench: Bench,
    description: Description,
    *,
    params: ServoParams = DEFAULT_PARAMS,
) -> ExperimentPlan:
    """The default experiment set, sized from the bench limits; see the constants above.

    ``params`` is the model the sizing predicts with: the one the rehearsal
    uses. All experiments are centred where the arm hangs (zero), so that each
    ends there and the safe state does not drop the arm from a height.
    """
    limits = bench.limits
    load = PendulumLoad.from_bench(bench.load)
    low_rad, high_rad = command_band_rad(limits)
    centre_rad = min(max(0.0, low_rad), high_rad)
    room_rad = min(centre_rad - low_rad, high_rad - centre_rad)
    if room_rad <= 0:
        raise PlanError("the command range leaves no room for a step around zero")
    speed_cap = STEP_SPEED_SHARE * limits.speed_max_rad_s
    overshoot_cap = STEP_OVERSHOOT_SHARE * limits.follow_error_max_rad

    def step_ok(amplitude_rad: float) -> bool:
        speed, overshoot = _predict_step(params, load, centre_rad, amplitude_rad)
        return speed <= speed_cap and overshoot <= overshoot_cap

    largest = _largest(step_ok, room_rad)
    if largest <= 0:
        raise PlanError(
            "no step fits: even a step of 0.1 deg is predicted to move faster than"
            f" {STEP_SPEED_SHARE:.0%} of speed_max"
        )
    budget_s = DURATION_SHARE * limits.experiment_max_s
    dwell_s = min(DWELL_S, (budget_s - SETTLE_S) / (2 * len(FIT_STEP_SHARES)))
    if dwell_s < DWELL_MIN_S:
        raise PlanError(
            f"experiment_max {_s(limits.experiment_max_s)} leaves less than {_s(DWELL_MIN_S)} to"
            f" hold each of the {2 * len(FIT_STEP_SHARES)} steps"
        )
    fit_steps = Step(
        centre_rad,
        tuple(_round(share * largest) for share in FIT_STEP_SHARES),
        dwell_s,
        SETTLE_S,
    )
    holdout = Step(
        centre_rad,
        tuple(_round(share * largest) for share in HOLDOUT_STEP_SHARES),
        dwell_s,
        SETTLE_S,
    )

    rate_hz = description.max_sample_rate_hz
    top_hz = min(SINE_HZ_MAX, rate_hz / SINE_SAMPLES_PER_CYCLE)
    frequencies = tuple(
        round(top_hz * SINE_SPAN ** (k / (SINE_FREQUENCIES - 1) - 1), 3)
        for k in range(SINE_FREQUENCIES)
    )
    command_cap_rad = SINE_COMMAND_SPEED_SHARE * limits.speed_max_rad_s / (2 * math.pi * top_hz)
    sine_cap = min(command_cap_rad, largest)

    def sine_ok(amplitude_rad: float) -> bool:
        return _predict_sine(params, load, centre_rad, amplitude_rad, frequencies) <= speed_cap

    sine_amplitude = _round(_largest(sine_ok, sine_cap))
    if sine_amplitude <= 0:
        raise PlanError("no sine amplitude keeps the predicted speed inside the limits")
    cycles = SINE_CYCLES
    sine = SteppedSine(
        centre_rad, sine_amplitude, frequencies, cycles, SINE_TRANSIENT_CYCLES, SETTLE_S
    )
    if sine.planned_duration_s > budget_s:
        raise PlanError(
            f"the stepped sine lasts {_s(sine.planned_duration_s)}, more than"
            f" {DURATION_SHARE:.0%} of experiment_max {_s(limits.experiment_max_s)}"
        )
    sweep_amplitude = _round(min(SWEEP_ROOM_SHARE * room_rad, SWEEP_AMPLITUDE_MAX_RAD))
    sweep_hz = min(
        SWEEP_HZ_MAX,
        SINE_COMMAND_SPEED_SHARE * limits.speed_max_rad_s / (2 * math.pi * sweep_amplitude),
    )
    sweep_hz = math.floor(sweep_hz * 1000) / 1000
    sweep = SteppedSine(centre_rad, sweep_amplitude, (sweep_hz,), 1, 0, SETTLE_S)
    if _predict_sine(params, load, centre_rad, sweep_amplitude, (sweep_hz,)) > speed_cap:
        raise PlanError("the slow sweep is predicted to move faster than the speed limit allows")
    if sweep.planned_duration_s > budget_s:
        raise PlanError(
            f"the slow sweep lasts {_s(sweep.planned_duration_s)}, more than"
            f" {DURATION_SHARE:.0%} of experiment_max {_s(limits.experiment_max_s)}"
        )
    for spec in (fit_steps, sine, sweep, holdout):
        problems = spec.check(limits)
        if problems:
            raise PlanError("; ".join(problems))

    design = {
        "centre_rad": centre_rad,
        "centre_why": "zero, where the arm hangs: each experiment ends there, so the safe state"
        " (torque off) does not drop the arm",
        "largest_step_rad": largest,
        "largest_step_why": (
            f"the largest step whose peak speed, predicted by the model the rehearsal uses, is"
            f" at most {STEP_SPEED_SHARE:.0%} of speed_max ({_deg_s(speed_cap)}) and whose"
            f" overshoot is at most {STEP_OVERSHOOT_SHARE:.0%} of follow_error_max"
            f" ({_deg(overshoot_cap)}), inside the command range"
            f" ({_deg(low_rad)} to {_deg(high_rad)}); rounded down to 0.1 deg"
        ),
        "fit_step_shares": list(FIT_STEP_SHARES),
        "holdout_step_shares": list(HOLDOUT_STEP_SHARES),
        "dwell_s": dwell_s,
        "dwell_why": f"{_s(DWELL_S)}, or less if experiment_max needs it (at least"
        f" {_s(DWELL_MIN_S)}), so that each step settles before the next",
        "sine_frequencies_hz": list(frequencies),
        "sine_frequencies_why": (
            f"{SINE_FREQUENCIES} log-spaced over a factor of {SINE_SPAN:g} up to the lower of"
            f" {SINE_HZ_MAX:g} Hz and the adapter's rate / {SINE_SAMPLES_PER_CYCLE}"
            f" ({_hz(rate_hz)} / {SINE_SAMPLES_PER_CYCLE})"
        ),
        "sine_amplitude_rad": sine_amplitude,
        "sine_amplitude_why": (
            f"the commanded speed at {_hz(top_hz)} at most {SINE_COMMAND_SPEED_SHARE:.0%} of"
            " speed_max, the predicted response speed at most"
            f" {STEP_SPEED_SHARE:.0%} of it, and no larger than the largest step"
        ),
        "sweep_amplitude_rad": sweep_amplitude,
        "sweep_frequency_hz": sweep_hz,
        "sweep_why": (
            f"one cycle; amplitude {SWEEP_ROOM_SHARE:.0%} of the command range on the narrower"
            f" side of zero, at most {_deg(SWEEP_AMPLITUDE_MAX_RAD)}; at most"
            f" {SWEEP_HZ_MAX:g} Hz, slower if the commanded speed would pass"
            f" {SINE_COMMAND_SPEED_SHARE:.0%} of speed_max. It swings the arm through a large"
            " gravity torque, which is what fixes the scale of the gains, inertia and friction"
        ),
        "rehearsal_and_sizing_params": records.plain(params),
        "note": "The sizing is a prediction with the default placeholder model. The"
        " rehearsal enforces the limits on that model, and the safety monitor on the real"
        " actuator; the margins leave room for a servo up to about 25 % faster.",
    }
    return ExperimentPlan(
        (
            PlannedExperiment(
                "steps", FIT, fit_steps, "steps at several amplitudes, both directions; fitted"
            ),
            PlannedExperiment(
                "sine", FIT, sine, "a stepped sine across the servo's bandwidth; fitted"
            ),
            PlannedExperiment(
                "sweep",
                FIT,
                sweep,
                "one slow, large sine through the load's gravity torque; fitted",
            ),
            PlannedExperiment(
                "holdout",
                HOLDOUT,
                holdout,
                "steps at other amplitudes, held out of the fit: the validation",
            ),
        ),
        design,
    )


def _predict_step(
    params: ServoParams, load: PendulumLoad, centre_rad: float, amplitude_rad: float
) -> tuple[float, float]:
    """(peak speed, overshoot past the target) of one step, predicted by the model."""
    times = np.arange(0.0, 1.0, 0.001)
    response = simulate(
        params, load, [0.0], [centre_rad + amplitude_rad], times, initial_position_rad=centre_rad
    )
    speed = float(np.max(np.abs(response.velocity_rad_s)))
    overshoot = float(np.max(np.abs(response.position_rad - centre_rad)) - abs(amplitude_rad))
    return speed, max(overshoot, 0.0)


def _predict_sine(
    params: ServoParams,
    load: PendulumLoad,
    centre_rad: float,
    amplitude_rad: float,
    frequencies_hz: tuple[float, ...],
) -> float:
    """Peak speed of the stepped sine, predicted by the model, commanded at 200 Hz."""
    peak = 0.0
    for frequency in frequencies_hz:
        command_times = np.arange(0.0, 2.0 / frequency, 0.005)
        commands = centre_rad + amplitude_rad * np.sin(2 * math.pi * frequency * command_times)
        response = simulate(
            params, load, command_times, commands, command_times, initial_position_rad=centre_rad
        )
        peak = max(peak, float(np.max(np.abs(response.velocity_rad_s))))
    return peak


def _largest(ok: Callable[[float], bool], cap_rad: float) -> float:
    """The largest amplitude up to ``cap_rad`` that ``ok`` accepts (bisection)."""
    if ok(cap_rad):
        return cap_rad
    low, high = 0.0, cap_rad
    for _ in range(30):
        middle = 0.5 * (low + high)
        low, high = (middle, high) if ok(middle) else (low, middle)
    return low


def _round(value_rad: float) -> float:
    steps = math.floor(abs(value_rad) / AMPLITUDE_ROUNDING_RAD + 1e-9)
    return math.copysign(steps * AMPLITUDE_ROUNDING_RAD, value_rad)


# -- the command -----------------------------------------------------------------------


def run_characterize(
    bench: Bench,
    *,
    runs_dir: str | Path = records.RUNS_DIR,
    adapter: Adapter | None = None,
) -> CommandResult:
    """Characterize the bench file's actuator; see the module text.

    ``adapter`` is opened from the bench file when not given. Every outcome is
    returned as a result, an unexpected exception included; only an unknown
    adapter name is raised (``UnknownAdapter``), as a usage error.
    """
    job = _Characterize(bench, Path(runs_dir))
    opened = adapter is None
    try:
        if bench.load is None:
            return job.no_load()
        if adapter is None:
            try:
                adapter = open_adapter(bench)
            except AdapterError as error:
                return job.unreachable(error)
        try:
            return job.run(adapter)
        finally:
            if opened:
                adapter.close()
    except UnknownAdapter:
        raise
    except Exception as error:  # noqa: BLE001 - reported in full in the result
        return job.crashed(error)


class _Characterize:
    def __init__(self, bench: Bench, runs_dir: Path) -> None:
        self.bench = bench
        self.runs_dir = runs_dir
        self.description: Description | None = None
        self.check: CommandResult | None = None
        self.plan: ExperimentPlan | None = None
        self.results: dict[str, RunResult] = {}
        self.parts: dict[str, str] = {}  # experiment name -> run dir
        self.files: dict[str, str] = {}
        self.notes: list[str] = []

    # -- outcomes before or instead of a fit ----------------------------------------------

    def no_load(self) -> CommandResult:
        return CommandResult(
            command=COMMAND,
            verdict=REFUSED,
            summary=f"Refused: {self.bench.source} has no [load]. characterize needs the load"
            " on the actuator: its known gravity torque is what separates the servo's inertia"
            " from its gains. Nothing was contacted.",
            exit_code=ExitCode.USAGE,
            needs_human=True,
            next="Tell the owner: characterize needs the pendulum arm fitted and described in"
            " bench.toml's [load] (mass and length to its centre of mass). The bench file is"
            " theirs to change.",
            metrics={"problems": [f"{self.bench.source}: no [load] section"]},
        )

    def unreachable(self, error: AdapterError) -> CommandResult:
        return CommandResult(
            command=COMMAND,
            verdict=ERROR,
            summary=f"Error: the actuator could not be reached: {type(error).__name__}: {error}.",
            exit_code=ExitCode.DEVICE,
            needs_human=True,
            next="Nothing was moved. Tell the human: check that the actuator is powered and"
            " connected, and that bench.toml's [target] names the right adapter, port and id.",
        )

    def crashed(self, error: BaseException) -> CommandResult:
        done = ", ".join(f"{name}: {r.outcome.value}" for name, r in self.results.items())
        state = (
            f"The experiments that ran ({done}) each ended in the runner, which puts the actuator"
            " in its safe state before it returns."
            if done
            else "No characterize experiment had run."
        )
        result = CommandResult(
            command=COMMAND,
            verdict=ERROR,
            summary=f"Error: characterize failed with an unexpected {type(error).__name__}:"
            f" {error}.",
            exit_code=ExitCode.DEVICE,
            needs_human=True,
            next="This is a fault in servobench, not a finding about the actuator. Tell the"
            f" human and give them the traceback under error. {state}",
            metrics=self.base_metrics(),
            error=error_details(error),
        )
        self.write_record(result)
        return result

    # -- the sequence ---------------------------------------------------------------------

    def run(self, adapter: Adapter) -> CommandResult:
        started = time.perf_counter()
        bench = self.bench
        self.check = run_check(bench, motion=True, runs_dir=self.runs_dir, adapter=adapter)
        if self.check.verdict != PASS:
            return self.not_checked()
        self.description = adapter.describe()
        try:
            self.plan = plan_experiments(bench, self.description)
        except PlanError as problem:
            return self.finish(
                CommandResult(
                    command=COMMAND,
                    verdict=REFUSED,
                    summary=f"Refused: the bench limits leave no room for characterize's"
                    f" experiments: {problem}. Nothing was moved after check.",
                    exit_code=ExitCode.USAGE,
                    needs_human=True,
                    next="Tell the owner what the limits refused. Do not change bench.toml to"
                    " get past it; the limits are theirs.",
                )
            )

        # check's moves ended just before it returned; hold the cool-down from then.
        last_end_s = adapter.clock.now_s()
        for planned in self.plan.experiments:
            self.wait_cooldown(adapter, last_end_s)
            result = run_experiment(adapter, bench, planned.spec)
            ended = utc_now()
            self.results[planned.name] = result
            analysis = None
            if result.trace is not None and result.trace.t_start_s is not None:
                try:
                    analysis = analyse(result.trace, planned.spec)
                except Exception as error:  # noqa: BLE001 - noted, the record still written
                    self.notes.append(f"{planned.name}: analysis failed: {error!r}")
            judgement = judge_run(result)
            path = records.write_run(
                result,
                command=COMMAND,
                runs_dir=self.runs_dir,
                verdict=None if judgement is None else judgement.verdict,
                metrics={
                    "characterize_experiment": planned.name,
                    "role": planned.role,
                    **({} if analysis is None else {analysis_key(analysis): as_metrics(analysis)}),
                },
                ended_utc=ended,
            )
            self.parts[planned.name] = str(path)
            self.add_part_files(path)
            end_s = result.safe_state.t_s
            last_end_s = end_s if end_s is not None else adapter.clock.now_s()
            if judgement is not None:
                return self.stopped(planned, result, judgement)
        return self.fit_and_report(time.perf_counter() - started)

    def wait_cooldown(self, adapter: Adapter, last_end_s: float) -> None:
        """Wait, on the adapter's clock and with the actuator in its safe state, for the rest."""
        due_s = last_end_s + self.bench.limits.cooldown_s
        rest_s = due_s - adapter.clock.now_s()
        if rest_s <= 0:
            return
        adapter.clock.wait_until_s(due_s)
        simulated = self.description is not None and self.description.identity.simulated
        self.notes.append(
            f"waited {_s(rest_s)} for the cool-down before the next experiment"
            + (" (virtual time on the simulated servo)" if simulated else "")
        )

    def not_checked(self) -> CommandResult:
        check = self.check
        if check.verdict == "partial":
            verdict, code, human = REFUSED, ExitCode.SAFETY, True
            next_ = (
                "check did not establish everything characterize relies on (see its items);"
                f" nothing more was moved. {check.next or ''} Tell the human what check could"
                " not decide."
            ).strip()
        else:
            verdict, code, human = check.verdict, check.exit_code, check.needs_human
            next_ = (
                f"{check.next or ''} characterize runs only on an actuator that passes check:"
                " a reversed, wrongly zeroed or faulty actuator gives data that a fit would turn"
                " into confident nonsense."
            ).strip()
        result = CommandResult(
            command=COMMAND,
            verdict=verdict,
            summary=f"Not characterized: check's verdict was {check.verdict}, and characterize"
            f" runs only after check passes. check: {check.summary}",
            exit_code=code,
            needs_human=human,
            next=next_,
        )
        return self.finish(result)

    def stopped(self, planned: PlannedExperiment, result: RunResult, judgement) -> CommandResult:
        names = [p.name for p in self.plan.experiments]
        number = names.index(planned.name) + 1
        skipped = names[number:]
        later = f"; {', '.join(skipped)} not run" if skipped else ""
        return self.finish(
            CommandResult(
                command=COMMAND,
                verdict=judgement.verdict,
                summary=f"Characterize stopped at experiment {number} of {len(names)}"
                f" ({planned.name}){later}, and nothing was fitted. {result.summary}",
                exit_code=judgement.exit_code,
                needs_human=judgement.needs_human,
                next=judgement.next,
            )
        )

    # -- the fit --------------------------------------------------------------------------

    def fit_and_report(self, experiments_s: float) -> CommandResult:
        bench, plan = self.bench, self.plan
        load = PendulumLoad.from_bench(bench.load)
        traces = {name: r.trace for name, r in self.results.items()}
        data = {name: FitExperiment.from_trace(name, trace) for name, trace in traces.items()}
        fitted = [data[p.name] for p in plan.experiments if p.role == FIT]
        held_out = next(p for p in plan.experiments if p.role == HOLDOUT)

        started = time.perf_counter()
        fit = fit_model(fitted, load)
        fit_s = time.perf_counter() - started

        holdout = data[held_out.name]
        default_error = prediction_error(DEFAULT_PARAMS, load, holdout)
        fitted_error = prediction_error(fit.prediction_params, load, holdout)
        floor = self.noise_floor(traces)
        ratio = fitted_error["rms_error_rad"] / floor.noise_floor_rad
        default_ratio = default_error["rms_error_rad"] / floor.noise_floor_rad
        quality = "good" if ratio <= GOOD_FIT_NOISE_RATIO else "poor"
        improvement = default_error["rms_error_rad"] / fitted_error["rms_error_rad"]
        validation = {
            "experiment": held_out.name,
            "default_model": default_error,
            "fitted_model": fitted_error,
            "noise_floor_rad": floor.noise_floor_rad,
            "noise_floor": records.plain(floor),
            "fitted_error_to_noise_floor": ratio,
            "default_error_to_noise_floor": default_ratio,
            "default_error_to_fitted_error": improvement,
            "fit_quality": quality,
            "fit_quality_rule": f"good when the fitted model's RMS error on the held-out"
            f" experiment is at most {GOOD_FIT_NOISE_RATIO:g} times the noise floor, poor"
            " otherwise",
        }
        estimates_found = fit.estimates
        if quality == "poor":
            # Determined by the data under a model that does not describe the
            # actuator: say so on every number, not only in the verdict.
            estimates_found = {
                name: dataclasses.replace(e, reason=POOR_FIT_PREFIX + e.reason)
                if e.identifiable
                else e
                for name, e in fit.estimates.items()
            }
        conditions = self.conditions(load)
        written = utc_now().isoformat()
        fit_quality = {
            "quality": quality,
            "usable": quality == "good",
            "statement": self.quality_statement(quality, default_error, fitted_error, floor),
            "held_out_experiment": held_out.name,
            "fitted_rms_error_rad": fitted_error["rms_error_rad"],
            "default_rms_error_rad": default_error["rms_error_rad"],
            "noise_floor_rad": floor.noise_floor_rad,
            "fitted_error_to_noise_floor": ratio,
            "rule": validation["fit_quality_rule"],
        }
        exported = export.export_json(
            estimates_found, fit_quality=fit_quality, conditions=conditions, written_utc=written
        )
        mujoco = None
        if quality == "good":
            identity = self.description.identity
            class_name = f"servobench_{identity.adapter}_{identity.device_id or 'device'}"
            mujoco = export.mujoco_fragment(
                fit.estimates, class_name=class_name, conditions=conditions
            )
        else:
            self.notes.append(
                f"{export.MUJOCO_FILE} was not written: the fitted model does not describe this"
                " actuator well enough to load into a simulator."
            )
        overlay = self.overlay_data(fit, load, holdout)

        estimates = {name: e.to_json() for name, e in estimates_found.items()}
        identifiable = [name for name in PARAMETERS if fit.estimates[name].identifiable]
        missing = [name for name in PARAMETERS if not fit.estimates[name].identifiable]
        metrics = self.base_metrics()
        metrics.update(
            {
                "parameters": estimates,
                "parameters_usable": quality == "good",
                "identifiable": identifiable,
                "not_identifiable": missing,
                "combinations": {DAMPING_TOTAL: estimates[DAMPING_TOTAL]},
                "validation": validation,
                "fit": self.fit_metrics(fit, fit_s),
                "response": self.response_metrics(),
                "conditions": conditions,
                "timing": {
                    "experiments_real_s": experiments_s,
                    "fit_real_s": fit_s,
                },
            }
        )
        verdict, code, human, summary, next_ = self.judge(
            quality, missing, fit, default_error, fitted_error, floor, improvement
        )
        result = CommandResult(
            command=COMMAND,
            verdict=verdict,
            summary=summary,
            exit_code=code,
            needs_human=human,
            next=next_,
            metrics=metrics,
        )
        return self.finish(result, exported=exported, mujoco=mujoco, overlay=overlay)

    def judge(self, quality, missing, fit, default_error, fitted_error, floor, improvement):
        fitted_deg = _deg(fitted_error["rms_error_rad"])
        default_deg = _deg(default_error["rms_error_rad"])
        floor_deg = _deg(floor.noise_floor_rad)
        ratio = fitted_error["rms_error_rad"] / floor.noise_floor_rad
        comparison = (
            f"On the held-out experiment the fitted model's RMS error is {fitted_deg}, the"
            f" default model's {default_deg}, against a noise floor of {floor_deg} in the"
            " readings"
        )
        if improvement >= 1.5:
            comparison += f": the fit predicts {improvement:.1f} times better than the default."
        elif improvement > 1 / 1.5:
            comparison += (
                ": the fit and the default predict it equally well, so the default parameters"
                " already describe this actuator."
            )
        else:
            comparison += ": the default predicts it better than the fit."
        if quality == "poor":
            summary = (
                f"Fail: the model does not describe this actuator well. {comparison} The fitted"
                f" model's error is {ratio:.1f} times the noise floor (a good fit is at most"
                f" {GOOD_FIT_NOISE_RATIO:g} times): the actuator does something the model does"
                " not have. The parameters are reported but must not be used as a simulator"
                " model."
            )
            next_ = (
                "This is a finding, not a failed run: report it to the human with the overlay"
                " plot (holdout_overlay.png) and the numbers. Do not rerun characterize to get a"
                " better fit, and do not change the experiments or the bench limits; the"
                " model's fit will not improve by retrying. Which effect the model is missing"
                " (backlash, voltage or temperature dependence, ...) is for the human to decide"
                " from the plot."
            )
            return FAIL, ExitCode.FAIL, True, summary, next_
        if missing:
            reasons = "; ".join(
                f"{name}: {fit.estimates[name].reason}" for name in missing
            )
            summary = (
                f"Partial: the fitted model describes the actuator, but {len(missing)} of"
                f" {len(PARAMETERS)} parameters are not identifiable from these experiments"
                f" ({', '.join(missing)}). {comparison}"
            )
            next_ = (
                "Report the identifiable parameters with their uncertainties, and the others as"
                " not identifiable with their reasons; that is the result, not a failure. Do"
                " not rerun or change the experiments to make a parameter look determined. "
                + reasons
            )
            return PARTIAL, ExitCode.PASS, False, summary, next_
        summary = f"Pass: every parameter was identified. {comparison}"
        return PASS, ExitCode.PASS, False, summary, None

    @staticmethod
    def quality_statement(quality, default_error, fitted_error, floor) -> str:
        text = (
            f"On an experiment held out of the fit, the fitted model's RMS error was"
            f" {_deg(fitted_error['rms_error_rad'])} and the default placeholder model's"
            f" {_deg(default_error['rms_error_rad'])}; the readings' noise floor was"
            f" {_deg(floor.noise_floor_rad)}."
        )
        if quality == "good":
            return text + " The fitted model describes the actuator to within the noise."
        return (
            text + " POOR FIT: the model does not describe this actuator; do not load these"
            " parameters into a simulator."
        )

    # -- pieces ---------------------------------------------------------------------------

    def noise_floor(self, traces):
        noises, resolutions = [], []
        for planned in self.plan.experiments:
            if not isinstance(planned.spec, Step):
                continue
            analysis = analyse(traces[planned.name], planned.spec)
            noises += [s.noise_rad for s in analysis.steps if s.noise_rad is not None]
            if analysis.quality.position_resolution_rad is not None:
                resolutions.append(analysis.quality.position_resolution_rad)
        return noise_floor(noises, min(resolutions) if resolutions else None)

    def conditions(self, load: PendulumLoad) -> dict[str, Any]:
        identity = self.description.identity
        samples = [s for r in self.results.values() for s in r.trace.samples]

        def span(name: str) -> dict[str, float | None]:
            values = [s.values[name] for s in samples if name in s.values]
            if not values:
                return {"min": None, "max": None, "mean": None}
            return {"min": min(values), "max": max(values), "mean": statistics.fmean(values)}

        return {
            "adapter": identity.adapter,
            "device_model": identity.model,
            "device_id": identity.device_id,
            "firmware": identity.firmware,
            "simulated": identity.simulated,
            "supply_voltage_v": span(VOLTAGE),
            "temperature_degc": span(TEMPERATURE),
            "load": {
                "mass_kg": load.mass_kg,
                "com_distance_m": load.com_distance_m,
                "inertia_kg_m2": load.inertia_kg_m2,
                "gravity_torque_max_n_m": load.gravity_torque_max_n_m,
                "assumed": "a point mass at com_distance_m, hanging straight down at position"
                " zero",
            },
            "tool_version": __version__,
            "bench_source": self.bench.source,
            "bench_fingerprint": self.bench.fingerprint,
            "measured_utc": utc_now().isoformat(),
            "experiments": {
                planned.name: {
                    "role": planned.role,
                    "run_dir": self.parts.get(planned.name),
                    "spec": planned.spec.to_dict(),
                }
                for planned in self.plan.experiments
            },
        }

    def fit_metrics(self, fit: FitResult, fit_s: float) -> dict[str, Any]:
        same = [s for s in fit.starts if s.same_minimum]
        return {
            "rms_error_rad": fit.rms_error_rad,
            "per_experiment": fit.per_experiment,
            "samples": fit.samples,
            "starts": [
                {
                    "start": s.start,
                    "end": s.end,
                    "rms_error_rad": s.rms_error_rad,
                    "same_minimum": s.same_minimum,
                    "evaluations": s.evaluations,
                }
                for s in fit.starts
            ],
            "starts_reaching_best": len(same),
            "starts_total": len(fit.starts),
            "coordinates_best": fit.best,
            "coordinates_uncertainty": {
                k: (v if math.isfinite(v) else None) for k, v in fit.coordinate_uncertainty.items()
            },
            "coordinates_uncertainty_note": "null: infinite, the readings do not change when"
            " the coordinate changes",
            "residual_lag1_correlation": fit.residual_lag1_correlation,
            "uncertainty_inflation": fit.uncertainty_inflation,
            "peak_torque_n_m": fit.peak_torque_n_m,
            "saturated": fit.saturated,
            "evaluations": fit.evaluations,
            "fit_real_s": fit_s,
            "prediction_params": records.plain(fit.prediction_params),
            "prediction_params_note": "the parameter set the fitted model was replayed with,"
            " for reproducing the validation. The values of parameters that are not"
            " identifiable are wherever the fit left them, which the data cannot tell from any"
            " other; they are not measurements.",
        }

    def response_metrics(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for planned in self.plan.experiments:
            trace = self.results[planned.name].trace
            analysis = analyse(trace, planned.spec)
            if isinstance(analysis, StepAnalysis):
                out[planned.name] = [
                    {
                        "label": s.label,
                        "commanded_step_rad": s.commanded_step_rad,
                        "measured_step_rad": s.measured_step_rad,
                        "delay_s": s.delay_s,
                        "rise_time_s": s.rise_time_s,
                        "overshoot_percent": s.overshoot_percent,
                        "settling_time_s": s.settling_time_s,
                        "steady_state_error_rad": s.steady_state_error_rad,
                    }
                    for s in analysis.steps
                ]
            elif isinstance(analysis, FrequencyAnalysis):
                out[planned.name] = [
                    {
                        "frequency_hz": p.frequency_hz,
                        "gain_db": p.gain_db,
                        "phase_rad": p.phase_rad,
                    }
                    for p in analysis.points
                ]
        out["note"] = (
            "the analyses' main numbers; every step's and frequency's full analysis, with"
            " uncertainties and the reason for any missing value, is in its experiment's run"
            " record (metrics.step_response or metrics.frequency_response)"
        )
        return out

    def overlay_data(self, fit: FitResult, load: PendulumLoad, holdout: FitExperiment):
        default = replay(DEFAULT_PARAMS, load, holdout).position_rad
        fitted = replay(fit.prediction_params, load, holdout).position_rad
        return {
            "times_s": holdout.sample_times_s,
            "measured_rad": holdout.positions_rad,
            "default_rad": default,
            "fitted_rad": fitted,
            "command_times_s": list(holdout.command_times_s),
            "command_rad": list(holdout.command_positions_rad),
        }

    def base_metrics(self) -> dict[str, Any]:
        check = self.check
        return {
            "check": None
            if check is None
            else {"verdict": check.verdict, "summary": check.summary, "run_dir": check.run_dir},
            "experiment_design": None if self.plan is None else self.plan.design,
            "experiments": {
                name: {
                    "outcome": result.outcome.value,
                    "summary": result.summary,
                    "run_dir": self.parts.get(name),
                    "cooled_s": result.cooled_s,
                }
                for name, result in self.results.items()
            },
            "notes": self.notes,
            "bench": {"source": self.bench.source, "fingerprint": self.bench.fingerprint},
            "limits": records.plain(self.bench.limits),
        }

    def add_part_files(self, path: Path) -> None:
        self.files[str(path / records.RUN_FILE)] = f"run record of {path.name}"

    # -- the record -----------------------------------------------------------------------

    def finish(self, result: CommandResult, *, exported=None, mujoco=None, overlay=None):
        if not result.metrics:
            result.metrics = self.base_metrics()
        if self.check is not None and self.check.run_dir:
            self.files[str(Path(self.check.run_dir) / records.RUN_FILE)] = (
                "check's run record (the gate before characterize moved the actuator)"
            )
        self.write_record(result, exported=exported, mujoco=mujoco, overlay=overlay)
        return result

    def write_record(self, result: CommandResult, *, exported=None, mujoco=None, overlay=None):
        description = self.description
        if description is None and self.check is None and not self.results:
            return  # nothing was contacted: nothing to record
        identity_source = description
        if identity_source is None and self.results:
            identity_source = next(iter(self.results.values())).description
        try:
            if identity_source is not None:
                adapter, device_id = (
                    identity_source.identity.adapter,
                    identity_source.identity.device_id,
                )
            else:
                target = self.bench.target
                adapter = target.adapter
                device_id = None if target.id is None else str(target.id)
            path = records.new_run_directory(COMMAND, adapter, device_id, self.runs_dir)
            own: dict[str, str] = {}
            if exported is not None:
                (path / export.JSON_FILE).write_text(
                    json.dumps(exported, indent=2, allow_nan=False) + "\n", encoding="utf-8"
                )
                own[export.JSON_FILE] = (
                    "fitted parameters, uncertainties, identifiability and conditions, for a"
                    " simulator"
                )
            if mujoco is not None:
                (path / export.MUJOCO_FILE).write_text(mujoco, encoding="utf-8")
                own[export.MUJOCO_FILE] = (
                    "MuJoCo fragment: a default class for the joint and its position actuator"
                )
            if overlay is not None:
                try:
                    validation = result.metrics["validation"]
                    plot_overlay(
                        path / "holdout_overlay.png",
                        title=f"characterize: held-out experiment on {adapter}"
                        f"{' ' + device_id if device_id else ''}"
                        + (" (simulated)" if identity_source.identity.simulated else ""),
                        default_error_rad=validation["default_model"]["rms_error_rad"],
                        fitted_error_rad=validation["fitted_model"]["rms_error_rad"],
                        noise_floor_rad=validation["noise_floor_rad"],
                        fit_quality=validation["fit_quality"],
                        **overlay,
                    )
                    own["holdout_overlay.png"] = (
                        "measured, default model and fitted model on the held-out experiment,"
                        " with each model's error"
                    )
                except Exception as error:  # noqa: BLE001 - the record says so
                    self.notes.append(f"the overlay plot could not be drawn: {error!r}")
            records.write_command_record(
                path,
                command=COMMAND,
                bench=self.bench,
                description=identity_source,
                verdict=result.verdict,
                summary=result.summary,
                metrics=dict(result.metrics, **({"error": result.error} if result.error else {})),
                files=own,
                parts=[*([self.check.run_dir] if self.check and self.check.run_dir else []),
                       *self.parts.values()],
                notes=self.notes,
            )  # fmt: skip
        except Exception as error:  # noqa: BLE001 - the result says so
            result.summary += (
                f" The characterize record could not be written: {type(error).__name__}: {error}."
            )
            result.needs_human = True
            if result.error is None:
                result.error = error_details(error)
            return
        result.run_dir = str(path)
        files = {str(path / records.RUN_FILE): "the characterize record (run.json)"}
        files.update({str(path / name): what for name, what in own.items()})
        files.update(self.files)
        result.files = files


def analysis_key(analysis) -> str:
    return "step_response" if isinstance(analysis, StepAnalysis) else "frequency_response"


def _deg(value_rad: float) -> str:
    return format_quantity(value_rad, Dimension.ANGLE, "deg", digits=3)


def _deg_s(value_rad_s: float) -> str:
    return format_quantity(value_rad_s, Dimension.ANGULAR_VELOCITY, "deg/s", digits=3)


def _hz(value_hz: float) -> str:
    return format_quantity(value_hz, Dimension.FREQUENCY, digits=3)


def _s(value_s: float) -> str:
    return format_quantity(value_s, Dimension.TIME, "s", digits=3)

