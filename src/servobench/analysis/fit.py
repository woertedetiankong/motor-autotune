"""Model fit and identifiability: the servo model's unknowns from recorded runs.

``fit_model(experiments, load)`` estimates the parameters of ``servobench.model``
(``ServoParams``) by least squares. For each candidate parameter set it replays
the commands each run actually sent, at the times the adapter recorded sending
them, through ``model.simulate``, from the state the run started in (the
position enable held, at rest), and compares the replayed position with every
recorded reading at its recorded timestamp. Nothing assumes even spacing, and
the quantised, noisy readings are used as they are.

What is fitted. The load is known from the bench file and is not fitted. The
fit's own coordinates are chosen so that a combination the data determines is
one coordinate, and a parameter it cannot determine is another:

====================  ==============================================================
coordinate            meaning
====================  ==============================================================
kp_n_m_rad            proportional gain
damping_n_m_s_rad     kd + viscous: the total torque per unit velocity
kd_share              kd / (kd + viscous), from 0 to 1
torque_max_n_m        the controller's torque limit
inertia_kg_m2         the servo's own inertia (the load's is added from the bench file)
coulomb_n_m           Coulomb friction
delay_s               command delay
====================  ==============================================================

Why these. While the controller's torque is inside its limit, the derivative
gain and viscous friction enter the equations only as their sum: both are a
torque proportional to velocity. They differ only while the torque is clamped
(the derivative term is clamped with it, viscous friction is not), so
``kd_share`` is determined only by an experiment that saturates. The torque
limit is likewise invisible until the controller reaches it. The servo's
inertia is separated from the gains by the load: the gravity torque
``m g L sin(theta)`` is known in newton metres, so the sag under it fixes the
gain in newton metres per radian, and the speed of the response then fixes the
inertia. Coulomb friction shows as the stick at reversals and the scatter of
where the servo comes to rest; the delay as the time before the response starts.

How well each is known. At the best fit, the Jacobian ``Jac`` of the residuals
with respect to the coordinates is computed by central differences, and the
covariance is ``s^2 (Jac^T Jac)^-1``, from its singular value decomposition, with
``s^2`` the residual variance. That formula assumes independent residuals, and
these are not: a quantised reading of a servo held still is off by the same
rounding error for as long as it is held, and a model of a real actuator
misses things that last. The covariance is therefore multiplied by
``N / N_eff``, estimated by batch means (``_inflation``): how much more the
means of blocks of 2 to 256 consecutive residuals scatter than independent
residuals' would, the largest factor over the block lengths. Lag-one
correlation alone, tried first, left the errors at about 1.7 times the
uncertainty on the simulated servo. A direction in which the residuals do not
change at all (a zero singular value) gives an infinite uncertainty to every
coordinate it involves.

The search. The fit starts from ``START_FACTORS`` around the default
placeholder parameters, each searched on a grid twice as coarse as the
model's (``SEARCH_DT_S``) to halve the cost, and once more from the best of
them with the torque limit below the largest torque that fit needed: the
hypothesis that the servo saturated. A fit in which the torque reaches its
limit is kept only if it beats the best one in which it does not by more than
``SAME_MINIMUM_SHARE``; if it does, every start is searched again with the
torque limit there, because while the servo saturates the delay, the torque
limit and the kd share trade off along a flat valley. The kept best is then
polished on the model's own grid, and every reported number comes from that.
Starts of the kept kind whose error is within ``SAME_MINIMUM_SHARE`` of the
best count as having found the same minimum; the spread of their values is
reported for each coordinate.

A parameter is reported as identifiable only when its relative standard
uncertainty is at most ``IDENTIFIABLE_REL_MAX`` (10 %) and the starts that
found the best minimum agree on it to within ``AGREEMENT_SIGMAS`` of that
uncertainty (or ``AGREEMENT_REL`` of its value). Otherwise it is not
identifiable, its value is None, and the reason says why; it is never filled in
with a guess. Two kinds of partial knowledge are still reported: a bound (the
torque limit is at least the largest torque the fitted model needed) and a
combination (``kd + viscous``) that is identifiable when its parts are not.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from scipy.optimize import least_squares

from servobench.analysis.quality import positions
from servobench.contract import POSITION
from servobench.experiments.runner import Trace
from servobench.model import DEFAULT_DT_S, DEFAULT_PARAMS, PendulumLoad, ServoParams, simulate
from servobench.units import Dimension, format_quantity

# The fit's coordinates, in order.
KP = "kp_n_m_rad"
DAMPING = "damping_n_m_s_rad"
KD_SHARE = "kd_share"
TORQUE_MAX = "torque_max_n_m"
INERTIA = "inertia_kg_m2"
COULOMB = "coulomb_n_m"
DELAY = "delay_s"
COORDINATES = (KP, DAMPING, KD_SHARE, TORQUE_MAX, INERTIA, COULOMB, DELAY)

# Combinations reported alongside the parameters.
DAMPING_TOTAL = "damping_total_n_m_s_rad"  # kd + viscous

# The ServoParams fields, in the order they are reported.
PARAMETERS = (
    "kp_n_m_rad",
    "kd_n_m_s_rad",
    "torque_max_n_m",
    "inertia_kg_m2",
    "viscous_n_m_s_rad",
    "coulomb_n_m",
    "delay_s",
)
UNITS = {
    "kp_n_m_rad": "N m/rad",
    "kd_n_m_s_rad": "N m s/rad",
    "torque_max_n_m": "N m",
    "inertia_kg_m2": "kg m^2",
    "viscous_n_m_s_rad": "N m s/rad",
    "coulomb_n_m": "N m",
    "delay_s": "s",
    DAMPING_TOTAL: "N m s/rad",
}
MEANINGS = {
    "kp_n_m_rad": "proportional gain of the servo's internal position controller",
    "kd_n_m_s_rad": "derivative gain of the servo's internal position controller, on velocity",
    "torque_max_n_m": "the controller's torque limit",
    "inertia_kg_m2": "the servo's own inertia reflected to the output, without the load",
    "viscous_n_m_s_rad": "viscous friction, including any back-EMF damping",
    "coulomb_n_m": "Coulomb (dry) friction, which also holds the output at rest",
    "delay_s": "from the adapter sending a command to the servo's controller acting on it",
    DAMPING_TOTAL: "kd + viscous: the total torque opposing velocity while the torque is"
    " inside its limit",
}

# Scale of each coordinate for the optimiser: the default placeholders.
_SCALE = {
    KP: DEFAULT_PARAMS.kp_n_m_rad,
    DAMPING: DEFAULT_PARAMS.kd_n_m_s_rad + DEFAULT_PARAMS.viscous_n_m_s_rad,
    KD_SHARE: 1.0,
    TORQUE_MAX: DEFAULT_PARAMS.torque_max_n_m,
    INERTIA: DEFAULT_PARAMS.inertia_kg_m2,
    COULOMB: DEFAULT_PARAMS.coulomb_n_m,
    DELAY: DEFAULT_PARAMS.delay_s,
}
# Bounds of each coordinate, in its own unit. Wide: they only keep the model physical.
_BOUNDS = {
    KP: (1e-4, 1e4),
    DAMPING: (0.0, 1e3),
    KD_SHARE: (0.0, 1.0),
    TORQUE_MAX: (1e-4, 1e4),
    INERTIA: (1e-9, 1e2),
    COULOMB: (0.0, 1e2),
    DELAY: (0.0, 0.1),
}

# Starting points: each coordinate as a multiple of the default placeholder.
# The default itself, then pairs that move the stiffness, damping, inertia,
# friction and delay in opposite directions, so that a valley in which two of
# them trade off shows up as starts that disagree.
START_FACTORS: tuple[dict[str, float], ...] = (
    {},
    {KP: 0.5, DAMPING: 2.0, INERTIA: 2.0, COULOMB: 0.3, DELAY: 2.0, KD_SHARE: 0.2},
    {KP: 2.0, DAMPING: 0.5, INERTIA: 0.5, COULOMB: 3.0, DELAY: 0.5, KD_SHARE: 0.9},
)
# The starts search on this grid, twice as coarse as the model's: the
# second-order scheme moves the minimum by about 0.1 % there, well inside the
# uncertainty, and the search costs half as much. The best is then polished on
# the model's own grid.
SEARCH_DT_S = 2 * DEFAULT_DT_S
# The saturation hypothesis: one more start with the torque limit at this share
# of the largest torque the best fit needed.
SATURATION_START_SHARE = 0.7

# Starts whose final cost is within this share of the best found the same minimum.
SAME_MINIMUM_SHARE = 0.01
# Identifiable: relative standard uncertainty at most this...
IDENTIFIABLE_REL_MAX = 0.10
# ... and the starts that found the best minimum agree to within this many
# standard uncertainties, or this share of the value, whichever is larger.
AGREEMENT_SIGMAS = 3.0
AGREEMENT_REL = 0.02
# A torque within this share of the limit counts as at the limit.
SATURATED_SHARE = 0.999

# The unused torque limit of a fit that does not saturate is set at least this
# far above the largest torque it needs.
UNUSED_LIMIT_FACTOR = 1.5
# Batch means for the uncertainty inflation: blocks up to this many residuals
# (256 readings is 1.3 s at 200 Hz, longer than a step's hold), and at least
# this many blocks.
INFLATION_BLOCK_MAX = 256
INFLATION_MIN_BLOCKS = 20
# The optimiser: tolerances on the cost, the step and the gradient, and the
# most residual evaluations per start (not counting its Jacobians).
_TOLERANCE = 1e-8
_MAX_ITERATIONS = 100

# Central-difference step for the Jacobian, as a share of each coordinate's scale.
_JACOBIAN_STEP = 1e-3
# Singular values below this share of the largest are treated as zero.
_SINGULAR_FLOOR = 1e-9


@dataclass(frozen=True)
class FitExperiment:
    """One run, ready for the fit: what was commanded, when, and what was read."""

    label: str
    t0_s: float  # when enable was called, on the adapter's clock
    initial_position_rad: float  # the reading enable held
    command_times_s: tuple[float, ...]
    command_positions_rad: tuple[float, ...]
    sample_times_s: np.ndarray  # readings taken during the plan
    positions_rad: np.ndarray

    @classmethod
    def from_trace(cls, label: str, trace: Trace) -> FitExperiment:
        if trace.t_enable_s is None or trace.enable_position_rad is None:
            raise ValueError(f"{label}: the actuator was never enabled; there is nothing to fit")
        times, values = positions(trace)
        if times.size == 0:
            raise ValueError(f"{label}: the run has no position readings")
        commands = [c for c in trace.commands if c.name == POSITION]
        return cls(
            label=label,
            t0_s=float(trace.t_enable_s),
            initial_position_rad=float(trace.enable_position_rad),
            command_times_s=tuple(c.t_s for c in commands),
            command_positions_rad=tuple(c.value for c in commands),
            sample_times_s=times,
            positions_rad=values,
        )

    @property
    def samples(self) -> int:
        return int(self.sample_times_s.size)


@dataclass(frozen=True)
class Replay:
    """The model's response to one experiment's commands, at its reading times."""

    position_rad: np.ndarray
    torque_n_m: np.ndarray


Simulator = Callable[..., Any]


def replay(
    params: ServoParams,
    load: PendulumLoad,
    experiment: FitExperiment,
    *,
    simulator: Simulator = simulate,
    dt_s: float = DEFAULT_DT_S,
) -> Replay:
    """Replay ``experiment``'s commands through the model with ``params``, on a ``dt_s`` grid."""
    times = np.concatenate(([experiment.t0_s], experiment.sample_times_s))
    response = simulator(
        params,
        load,
        experiment.command_times_s,
        experiment.command_positions_rad,
        times,
        initial_position_rad=experiment.initial_position_rad,
        initial_setpoint_rad=experiment.initial_position_rad,
        dt_s=dt_s,
    )
    return Replay(response.position_rad[1:], response.torque_n_m[1:])


def prediction_error(
    params: ServoParams,
    load: PendulumLoad,
    experiment: FitExperiment,
    *,
    simulator: Simulator = simulate,
) -> dict[str, float]:
    """How far the model's replay is from the readings: RMS and largest, in radians."""
    error = replay(params, load, experiment, simulator=simulator).position_rad
    error = error - experiment.positions_rad
    return {
        "rms_error_rad": float(np.sqrt(np.mean(error * error))),
        "max_error_rad": float(np.max(np.abs(error))),
        "samples": experiment.samples,
    }


# -- coordinates -----------------------------------------------------------------------


def coordinates_of(params: ServoParams) -> dict[str, float]:
    damping = params.kd_n_m_s_rad + params.viscous_n_m_s_rad
    return {
        KP: params.kp_n_m_rad,
        DAMPING: damping,
        KD_SHARE: params.kd_n_m_s_rad / damping if damping > 0 else 0.5,
        TORQUE_MAX: params.torque_max_n_m,
        INERTIA: params.inertia_kg_m2,
        COULOMB: params.coulomb_n_m,
        DELAY: params.delay_s,
    }


def params_of(values: dict[str, float]) -> ServoParams:
    damping, share = values[DAMPING], values[KD_SHARE]
    return ServoParams(
        kp_n_m_rad=values[KP],
        kd_n_m_s_rad=share * damping,
        torque_max_n_m=values[TORQUE_MAX],
        inertia_kg_m2=values[INERTIA],
        viscous_n_m_s_rad=(1.0 - share) * damping,
        coulomb_n_m=values[COULOMB],
        delay_s=values[DELAY],
    )


def _to_x(values: dict[str, float]) -> np.ndarray:
    return np.array([values[name] / _SCALE[name] for name in COORDINATES])


def _from_x(x: np.ndarray) -> dict[str, float]:
    return {name: float(x[i]) * _SCALE[name] for i, name in enumerate(COORDINATES)}


def _clip(values: dict[str, float]) -> dict[str, float]:
    return {name: min(max(v, _BOUNDS[name][0]), _BOUNDS[name][1]) for name, v in values.items()}


# -- the fit ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Start:
    """One starting point and where it ended."""

    start: dict[str, float]
    end: dict[str, float]
    rms_error_rad: float
    evaluations: int
    same_minimum: bool  # the kind of fit kept, within SAME_MINIMUM_SHARE of the best cost
    saturated: bool  # whether its torque reached its limit
    peak_torque_n_m: float


@dataclass(frozen=True)
class _Run:
    start: dict[str, float]
    end: dict[str, float]
    rms_error_rad: float
    evaluations: int
    peak_torque_n_m: float
    saturated: bool


@dataclass(frozen=True)
class Estimate:
    """What the data says about one parameter or combination."""

    name: str
    unit: str
    meaning: str
    identifiable: bool
    value: float | None  # None when not identifiable: never filled in
    uncertainty: float | None  # one standard uncertainty, same unit; None with value
    reason: str  # why it is, or is not, identifiable
    relative_uncertainty: float | None = None
    # Partial knowledge, when not identifiable.
    lower_bound: float | None = None
    determined_only_as: str | None = None  # the combination that is determined
    # The fit's own number and its uncertainty, kept for the record when the
    # uncertainty is finite but too large: not a measurement.
    unreliable_estimate: float | None = None
    unreliable_uncertainty: float | None = None
    starts_spread: float | None = None  # largest - smallest over agreeing starts

    def to_json(self) -> dict[str, Any]:
        data = {
            "status": "identifiable" if self.identifiable else "not_identifiable",
            "value": self.value,
            "uncertainty": self.uncertainty,
            "relative_uncertainty": self.relative_uncertainty,
            "unit": self.unit,
            "meaning": self.meaning,
            "reason": self.reason,
        }
        for key in (
            "lower_bound",
            "determined_only_as",
            "unreliable_estimate",
            "unreliable_uncertainty",
            "starts_spread",
        ):
            value = getattr(self, key)
            if value is not None:
                data[key] = value
        return data


@dataclass(frozen=True)
class FitResult:
    """The outcome of ``fit_model``."""

    best: dict[str, float]  # coordinates of the best fit
    prediction_params: ServoParams  # the parameter set the fitted model replays with
    estimates: dict[str, Estimate]  # PARAMETERS and DAMPING_TOTAL
    coordinate_uncertainty: dict[str, float]  # one standard uncertainty, inf if undetermined
    starts: tuple[Start, ...]
    rms_error_rad: float  # over all fitted readings
    per_experiment: dict[str, dict[str, float]]  # label -> prediction_error
    residual_lag1_correlation: float
    uncertainty_inflation: float  # N / N_eff
    peak_torque_n_m: float  # the largest torque the best fit needed
    saturated: bool  # whether the best fit reached its torque limit
    samples: int
    evaluations: int  # model replays, all starts and the Jacobian included
    polish_evaluations: int = 0  # of those, the polish on the model's own grid
    notes: tuple[str, ...] = ()

    @property
    def identifiable(self) -> list[str]:
        return [name for name, e in self.estimates.items() if e.identifiable]

    @property
    def not_identifiable(self) -> list[str]:
        return [name for name, e in self.estimates.items() if not e.identifiable]


class _Problem:
    def __init__(
        self,
        experiments: Sequence[FitExperiment],
        load: PendulumLoad,
        simulator: Simulator,
        dt_s: float,
    ) -> None:
        self.experiments = tuple(experiments)
        self.load = load
        self.simulator = simulator
        self.dt_s = dt_s
        self.measured = np.concatenate([e.positions_rad for e in self.experiments])
        self.evaluations = 0

    def replays(self, values: dict[str, float]) -> list[Replay]:
        self.evaluations += 1
        params = params_of(values)
        return [
            replay(params, self.load, e, simulator=self.simulator, dt_s=self.dt_s)
            for e in self.experiments
        ]

    def residuals(self, x: np.ndarray) -> np.ndarray:
        replays = self.replays(_from_x(x))
        return np.concatenate([r.position_rad for r in replays]) - self.measured


def fit_model(
    experiments: Sequence[FitExperiment],
    load: PendulumLoad,
    *,
    simulator: Simulator = simulate,
    starts: Sequence[dict[str, float]] | None = None,
) -> FitResult:
    """Fit the model's unknowns to ``experiments``; see the module text.

    ``starts`` overrides the starting points (coordinates in their own units);
    the default is ``START_FACTORS`` around the default placeholders.
    ``simulator`` replaces ``model.simulate``, for studying the integrator.
    """
    if not experiments:
        raise ValueError("fit_model needs at least one experiment")
    search = _Problem(experiments, load, simulator, SEARCH_DT_S)
    problem = _Problem(experiments, load, simulator, DEFAULT_DT_S)
    lower = _to_x({name: _BOUNDS[name][0] for name in COORDINATES})
    upper = _to_x({name: _BOUNDS[name][1] for name in COORDINATES})
    if starts is None:
        starts = [_start(factors) for factors in START_FACTORS]

    def solve(start: dict[str, float], problem: _Problem = search) -> _Run:
        before = problem.evaluations
        x0 = np.clip(_to_x(_clip(start)), lower, upper)
        solution = least_squares(
            problem.residuals,
            x0,
            bounds=(lower, upper),
            method="trf",
            x_scale=1.0,
            diff_step=_JACOBIAN_STEP,
            ftol=_TOLERANCE,
            xtol=_TOLERANCE,
            gtol=_TOLERANCE,
            max_nfev=_MAX_ITERATIONS,
        )
        end = _from_x(solution.x)
        peak = _peak_torque(problem.replays(end))
        return _Run(
            start=dict(start),
            end=end,
            rms_error_rad=float(np.sqrt(2.0 * solution.cost / problem.measured.size)),
            evaluations=problem.evaluations - before,
            peak_torque_n_m=peak,
            saturated=peak >= SATURATED_SHARE * end[TORQUE_MAX],
        )

    runs: list[_Run] = []
    for start in starts:
        runs.append(solve(start))

    # The saturation hypothesis: once more from the best fit so far, with the
    # torque limit below the largest torque that fit needed.
    first = min(runs, key=lambda run: run.rms_error_rad)
    if not first.saturated and first.peak_torque_n_m > 0:
        clipped = SATURATION_START_SHARE * first.peak_torque_n_m
        hypothesis = solve(dict(first.end, **{TORQUE_MAX: clipped}))
        runs.append(hypothesis)
        if hypothesis.saturated and hypothesis.rms_error_rad**2 < first.rms_error_rad**2 * (
            1 - SAME_MINIMUM_SHARE
        ):
            # The servo did saturate. While it does, the delay, the torque
            # limit and the kd share trade off along a flat valley that one
            # start does not explore: start again from each of the starting
            # points, with the torque limit where the hypothesis found it, so
            # that the saturating fit has several starts to agree or disagree.
            limit = hypothesis.end[TORQUE_MAX]
            for start in starts:
                runs.append(solve(dict(start, **{TORQUE_MAX: limit})))

    # Which kind of fit to keep. A fit in which the torque reaches its limit is
    # kept only if it is better, by more than SAME_MINIMUM_SHARE, than the best
    # in which it does not: a limit that only just clips the largest peak
    # explains nothing the data needs.
    unsaturated = [run for run in runs if not run.saturated]
    saturating = [run for run in runs if run.saturated]
    best_free = min(unsaturated, key=lambda run: run.rms_error_rad, default=None)
    best_clipped = min(saturating, key=lambda run: run.rms_error_rad, default=None)
    saturated = best_clipped is not None and (
        best_free is None
        or best_clipped.rms_error_rad**2 < best_free.rms_error_rad**2 * (1 - SAME_MINIMUM_SHARE)
    )
    family = saturating if saturated else unsaturated
    found = best_clipped if saturated else best_free
    # The search ran on a coarse grid; polish its best on the model's own grid,
    # from which every reported number comes.
    chosen = solve(found.end, problem)
    if chosen.saturated != saturated:  # the polish crossed over: keep the search's kind
        chosen = _Run(found.start, found.end, found.rms_error_rad, 0, found.peak_torque_n_m,
                      found.saturated)  # fmt: skip
    best = dict(chosen.end)
    peak = chosen.peak_torque_n_m
    if not saturated:
        # Keep the unused limit well clear of the torques the fit needs, so that
        # the Jacobian's small steps cannot reach it.
        best[TORQUE_MAX] = max(best[TORQUE_MAX], UNUSED_LIMIT_FACTOR * peak)
    limit = found.rms_error_rad**2 * (1.0 + SAME_MINIMUM_SHARE)
    start_records = tuple(
        Start(
            run.start,
            run.end,
            run.rms_error_rad,
            run.evaluations,
            run in family and run.rms_error_rad**2 <= limit,
            run.saturated,
            run.peak_torque_n_m,
        )
        for run in runs
    )

    best_replays = problem.replays(best)
    residuals = np.concatenate([r.position_rad for r in best_replays]) - problem.measured
    jacobian = _jacobian(problem, best, lower, upper)
    sizes = [e.samples for e in problem.experiments]
    lag1 = _lag1(residuals, sizes)
    inflation = _inflation(residuals, sizes)
    dof = max(1, residuals.size - len(COORDINATES))
    variance = float(residuals @ residuals) / dof
    covariance = _covariance(jacobian, variance * inflation)
    sigma = {name: math.sqrt(covariance[i, i]) for i, name in enumerate(COORDINATES)}

    agreeing = [s.end for s in start_records if s.same_minimum]
    spread = {
        name: max(v[name] for v in agreeing) - min(v[name] for v in agreeing)
        for name in COORDINATES
    }
    estimates = _estimates(best, covariance, sigma, spread, len(agreeing), peak, saturated)

    prediction = params_of(best)
    per_experiment = {
        e.label: prediction_error(prediction, load, e, simulator=simulator)
        for e in problem.experiments
    }
    return FitResult(
        best=best,
        prediction_params=prediction,
        estimates=estimates,
        coordinate_uncertainty=sigma,
        starts=start_records,
        rms_error_rad=float(np.sqrt(np.mean(residuals * residuals))),
        per_experiment=per_experiment,
        residual_lag1_correlation=lag1,
        uncertainty_inflation=inflation,
        peak_torque_n_m=peak,
        saturated=saturated,
        samples=int(residuals.size),
        evaluations=search.evaluations + problem.evaluations,
        polish_evaluations=chosen.evaluations,
    )


def _start(factors: dict[str, float]) -> dict[str, float]:
    """A starting point: the default placeholders times ``factors``; kd_share is given as is."""
    default = coordinates_of(DEFAULT_PARAMS)
    start = {name: default[name] * factors.get(name, 1.0) for name in COORDINATES}
    start[KD_SHARE] = factors.get(KD_SHARE, default[KD_SHARE])
    return start


def _peak_torque(replays: Sequence[Replay]) -> float:
    return max(float(np.max(np.abs(r.torque_n_m))) for r in replays)


def _jacobian(
    problem: _Problem, values: dict[str, float], lower: np.ndarray, upper: np.ndarray
) -> np.ndarray:
    """d residual / d coordinate (in the coordinate's own unit), by central differences."""
    x = _to_x(values)
    columns = []
    for i, name in enumerate(COORDINATES):
        h = _JACOBIAN_STEP * max(abs(x[i]), 1.0 if name == KD_SHARE else 0.1)
        up, down = x.copy(), x.copy()
        up[i] = min(x[i] + h, upper[i])
        down[i] = max(x[i] - h, lower[i])
        span = (up[i] - down[i]) * _SCALE[name]
        columns.append((problem.residuals(up) - problem.residuals(down)) / span)
    return np.column_stack(columns)


def _lag1(residuals: np.ndarray, sizes: Sequence[int]) -> float:
    """The pooled lag-one autocorrelation of each experiment's residuals."""
    products, squares = 0.0, 0.0
    for part in _parts(residuals, sizes):
        products += float(part[1:] @ part[:-1])
        squares += float(part @ part)
    return products / squares if squares > 0 else 0.0


def _inflation(residuals: np.ndarray, sizes: Sequence[int]) -> float:
    """N / N_eff by batch means: how much more the residuals' block means scatter than
    independent residuals' would, for blocks of 1 to INFLATION_BLOCK_MAX residuals.

    Correlation over a long stretch, such as the constant rounding error of a
    quantised reading while the servo is held still, raises the scatter of the
    means of long blocks; lag-one correlation alone would not see it. The
    largest factor over the block lengths is used, from blocks of which there
    are at least INFLATION_MIN_BLOCKS.
    """
    parts = _parts(residuals, sizes)
    total = sum(float(part @ part) for part in parts)
    count = sum(part.size for part in parts)
    if total <= 0 or count == 0:
        return 1.0
    variance = total / count
    factor, block = 1.0, 2
    while block <= INFLATION_BLOCK_MAX:
        squares, blocks = 0.0, 0
        for part in parts:
            n = part.size // block
            if n:
                means = part[: n * block].reshape(n, block).mean(axis=1)
                squares += float(means @ means)
                blocks += n
        if blocks < INFLATION_MIN_BLOCKS:
            break
        factor = max(factor, block * (squares / blocks) / variance)
        block *= 2
    return factor


def _parts(residuals: np.ndarray, sizes: Sequence[int]) -> list[np.ndarray]:
    parts, start = [], 0
    for size in sizes:
        part = residuals[start : start + size]
        parts.append(part - part.mean())
        start += size
    return parts


def _covariance(jacobian: np.ndarray, variance: float) -> np.ndarray:
    """``variance (J^T J)^-1``, with an infinite variance along directions J does not see."""
    _, singular, vt = np.linalg.svd(jacobian, full_matrices=False)
    floor = _SINGULAR_FLOOR * (singular[0] if singular.size and singular[0] > 0 else 1.0)
    n = jacobian.shape[1]
    covariance = np.zeros((n, n))
    unseen = np.zeros(n, dtype=bool)
    for k, s in enumerate(singular):
        direction = vt[k]
        if s <= floor:
            unseen |= np.abs(direction) > 1e-6
            continue
        covariance += np.outer(direction, direction) * (variance / (s * s))
    covariance[unseen, :] = math.inf
    covariance[:, unseen] = math.inf
    return covariance


# -- what is identifiable ----------------------------------------------------------------


def _judge(
    value: float, sigma: float, spread: float, agreeing: int
) -> tuple[bool, float | None, str]:
    """Whether a value with standard uncertainty ``sigma`` is identifiable, and why."""
    if not math.isfinite(sigma):
        return False, None, "the readings do not change at all when it changes"
    relative = sigma / abs(value) if value != 0 else math.inf
    if relative > IDENTIFIABLE_REL_MAX:
        return (
            False,
            relative,
            f"it is determined only to +/- {relative:.0%} of its value (one standard"
            f" uncertainty), more than the {IDENTIFIABLE_REL_MAX:.0%} needed to report it",
        )
    allowed = max(AGREEMENT_SIGMAS * sigma, AGREEMENT_REL * abs(value))
    if agreeing > 1 and spread > allowed:
        return (
            False,
            relative,
            f"the {agreeing} starting points that reached the same fit error ended"
            f" {spread / abs(value):.0%} of its value apart, more than its uncertainty allows",
        )
    agree = (
        f"; the {agreeing} starting points that reached the best fit agree on it"
        if agreeing > 1
        else "; only one starting point reached the best fit"
    )
    return True, relative, f"determined to +/- {relative:.1%} (one standard uncertainty){agree}"


def _estimates(
    best: dict[str, float],
    covariance: np.ndarray,
    sigma: dict[str, float],
    spread: dict[str, float],
    agreeing: int,
    peak_torque_n_m: float,
    saturated: bool,
) -> dict[str, Estimate]:
    index = {name: i for i, name in enumerate(COORDINATES)}
    out: dict[str, Estimate] = {}

    def direct(name: str, coordinate: str) -> Estimate:
        value, s = best[coordinate], sigma[coordinate]
        ok, relative, reason = _judge(value, s, spread[coordinate], agreeing)
        if ok:
            return Estimate(
                name, UNITS[name], MEANINGS[name], True, value, s, reason, relative,
                starts_spread=spread[coordinate],
            )  # fmt: skip
        return Estimate(
            name, UNITS[name], MEANINGS[name], False, None, None, f"Not identifiable: {reason}.",
            unreliable_estimate=value if math.isfinite(s) else None,
            unreliable_uncertainty=s if math.isfinite(s) else None,
            starts_spread=spread[coordinate],
        )  # fmt: skip

    out["kp_n_m_rad"] = direct("kp_n_m_rad", KP)
    out["inertia_kg_m2"] = direct("inertia_kg_m2", INERTIA)
    out["coulomb_n_m"] = direct("coulomb_n_m", COULOMB)
    out["delay_s"] = direct("delay_s", DELAY)

    torque = direct("torque_max_n_m", TORQUE_MAX)
    if not torque.identifiable and not saturated:
        torque = Estimate(
            "torque_max_n_m", UNITS["torque_max_n_m"], MEANINGS["torque_max_n_m"], False,
            None, None,
            "Not identifiable: the servo never reached its torque limit in these experiments."
            f" The largest torque the fitted model needed was {_n_m(peak_torque_n_m)}, so the"
            " limit is only known to be at least that. Only an experiment that saturates the"
            " servo (a larger or faster step than the bench limits allow here) can measure it.",
            lower_bound=peak_torque_n_m,
        )  # fmt: skip
    out["torque_max_n_m"] = torque

    # The damping: the sum always, and its split into kd and viscous when the data has it.
    total = direct(DAMPING_TOTAL, DAMPING)
    out[DAMPING_TOTAL] = total
    share, s_share = best[KD_SHARE], sigma[KD_SHARE]
    damping = best[DAMPING]
    split_known = math.isfinite(s_share) and s_share * damping <= IDENTIFIABLE_REL_MAX * max(
        share * damping, (1 - share) * damping
    )
    for name, sign in (("kd_n_m_s_rad", 1.0), ("viscous_n_m_s_rad", -1.0)):
        part_share = share if sign > 0 else 1.0 - share
        value = part_share * damping
        # d value / d (damping, share) = (part_share, sign * damping)
        gradient = np.zeros(len(COORDINATES))
        gradient[index[DAMPING]] = part_share
        gradient[index[KD_SHARE]] = sign * damping
        variance = _quadratic(covariance, gradient)
        s = math.sqrt(variance) if math.isfinite(variance) else math.inf
        if split_known and total.identifiable:
            ok, relative, reason = _judge(value, s, 0.0, agreeing)
        else:
            ok, relative, reason = False, None, ""
        if ok:
            out[name] = Estimate(
                name, UNITS[name], MEANINGS[name], True, value, s, reason, relative
            )
            continue
        why_saturation = (
            "the controller's torque never reached its limit, and only while it is clamped do"
            " the two act differently (the derivative term is clamped with the rest of the"
            " controller's torque, viscous friction is not)"
            if not saturated
            else "the stretches where the controller's torque was clamped were too short to"
            " tell the two apart"
        )
        out[name] = Estimate(
            name, UNITS[name], MEANINGS[name], False, None, None,
            "Not identifiable on its own: only kd + viscous is determined"
            f" ({DAMPING_TOTAL}), because both are a torque proportional to velocity and"
            f" {why_saturation}.",
            determined_only_as=DAMPING_TOTAL,
            unreliable_estimate=value if math.isfinite(s) else None,
            unreliable_uncertainty=s if math.isfinite(s) else None,
        )  # fmt: skip
    return {name: out[name] for name in (*PARAMETERS, DAMPING_TOTAL)}


def _quadratic(covariance: np.ndarray, gradient: np.ndarray) -> float:
    used = gradient != 0
    block = covariance[np.ix_(used, used)]
    if not np.all(np.isfinite(block)):
        return math.inf
    g = gradient[used]
    return float(max(g @ block @ g, 0.0))


# -- the readings' own noise -------------------------------------------------------------


@dataclass(frozen=True)
class NoiseFloor:
    """The error a model that described the actuator exactly would still have."""

    noise_floor_rad: float  # sqrt(noise_rad^2 + offset_rad^2)
    noise_rad: float  # pooled standard deviation of the readings at rest
    offset_rad: float  # expected offset of a reading's mean from the truth, from the rounding
    resolution_rad: float | None  # the reading step; None if not seen
    windows: int  # how many stretches at rest it was measured over
    definition: str = field(
        default="sqrt(noise_rad^2 + offset_rad^2). noise_rad is the scatter of the readings"
        " about their mean at rest, pooled over the settled end of each step. offset_rad is"
        " the root-mean-square offset of that mean from the true position that rounding to"
        " the reading step q leaves when the readings are dithered by noise of that size:"
        " offset_rad^2 = sum over k of q^2 / (2 pi^2 k^2) exp(-4 pi^2 k^2 noise_rad^2 / q^2),"
        " which is q^2 / 12 with no noise and falls quickly once the noise is a third of a"
        " step or more. It treats the scatter of the rounded readings as the noise before"
        " rounding, an estimate. On the simulated servo, on the held-out experiment, the fitted"
        " model of a servo that the model describes exactly scored 0.89 to 1.12 of it with"
        " noise and jitter, and 0.64 to 0.76 without noise, where the fit absorbs part of the"
        " rounding."
    )


def noise_floor(noises_rad: Sequence[float], resolution_rad: float | None) -> NoiseFloor:
    """From each settled window's standard deviation and the reading step."""
    values = [n for n in noises_rad if n is not None and math.isfinite(n)]
    noise = math.sqrt(sum(n * n for n in values) / len(values)) if values else 0.0
    offset_squared = 0.0
    if resolution_rad:
        q = resolution_rad
        for k in range(1, 200):
            offset_squared += q * q / (2 * math.pi**2 * k * k) * math.exp(
                -4 * math.pi**2 * k * k * noise * noise / (q * q)
            )
    offset = math.sqrt(offset_squared)
    return NoiseFloor(
        math.sqrt(noise * noise + offset_squared), noise, offset, resolution_rad, len(values)
    )


def _n_m(value: float) -> str:
    return format_quantity(value, Dimension.TORQUE, digits=3)
