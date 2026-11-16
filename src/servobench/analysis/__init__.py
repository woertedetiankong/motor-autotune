"""Analysis of recorded runs: step response and frequency response.

Each analysis takes the runner's ``Trace`` (live from ``run_experiment``, or
loaded back with ``records.load_run``) and the experiment spec, and needs no
adapter. It returns frozen dataclasses; ``as_metrics`` turns any of them into
plain JSON data for a command's output and a run record's ``metrics``.

Every value is SI under a name that carries its unit, except where the name
says otherwise (``overshoot_percent``, ``gain_db``, ``gain`` and ``step_ratio``,
which are ratios). Every value that could not be measured is None, and the
result's ``reasons`` holds, under the same name, why. Each result also says how
good its inputs were: the timing actually achieved, the reading's resolution
and noise, and an uncertainty for each metric.
"""

from __future__ import annotations

import dataclasses
import math
from typing import Any

import numpy as np

from servobench.analysis.frequency import FrequencyAnalysis, FrequencyPoint, analyse_stepped_sine
from servobench.analysis.quality import Timing, TraceQuality, timing_of, trace_quality
from servobench.analysis.step import StepAnalysis, StepResponse, analyse_step
from servobench.experiments.runner import Trace
from servobench.experiments.spec import ExperimentSpec, Step, SteppedSine

__all__ = [
    "FrequencyAnalysis",
    "FrequencyPoint",
    "StepAnalysis",
    "StepResponse",
    "Timing",
    "TraceQuality",
    "analyse",
    "analyse_step",
    "analyse_stepped_sine",
    "as_metrics",
    "timing_of",
    "trace_quality",
]

STEP_RESPONSE = "step_response"
FREQUENCY_RESPONSE = "frequency_response"


def analyse(trace: Trace, spec: ExperimentSpec) -> StepAnalysis | FrequencyAnalysis:
    """The analysis that suits ``spec``: step response for a step, frequency response for a sine."""
    if isinstance(spec, Step):
        return analyse_step(trace, spec)
    if isinstance(spec, SteppedSine):
        return analyse_stepped_sine(trace, spec)
    raise TypeError(f"there is no response analysis for a {spec.kind} experiment")


def as_metrics(result: StepAnalysis | FrequencyAnalysis) -> dict[str, Any]:
    """``result`` as plain JSON data: objects, lists, strings, finite numbers and null."""
    if isinstance(result, StepAnalysis):
        kind = STEP_RESPONSE
    elif isinstance(result, FrequencyAnalysis):
        kind = FREQUENCY_RESPONSE
    else:
        raise TypeError(f"not an analysis result: {type(result).__name__}")
    return {"analysis": kind, **_plain(dataclasses.asdict(result))}


def _plain(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    if isinstance(value, (int, np.integer)):
        return int(value)
    if isinstance(value, (float, np.floating)):
        if not math.isfinite(value):
            raise ValueError(f"a metric is not a finite number: {value!r}")
        return float(value)
    return value
