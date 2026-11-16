"""Analysis results as data: from a run record, into JSON, with a reason for every gap."""

from __future__ import annotations

import dataclasses
import json
import math

import pytest
from analysis_support import DEG, NOISELESS, completed, run

from servobench.analysis import (
    FREQUENCY_RESPONSE,
    STEP_RESPONSE,
    StepResponse,
    analyse,
    as_metrics,
)
from servobench.analysis.quality import timing_of
from servobench.experiments.spec import Hold, Step, SteppedSine
from servobench.records import load_run, write_run

STEP = Step(0.0, (10 * DEG, -10 * DEG, 1 * DEG), dwell_s=1.0, settle_s=0.5)
SINE = SteppedSine(0.0, 1.5 * DEG, (1.0, 4.0, 15.0), cycles=4, transient_cycles=1, settle_s=0.5)


@pytest.mark.parametrize("spec", [STEP, SINE], ids=["step", "stepped sine"])
def test_a_run_record_loaded_back_gives_the_same_analysis(spec, tmp_path):
    result = run(spec, seed=4)
    live = analyse(result.trace, spec)
    path = write_run(result, command="characterize", runs_dir=tmp_path, metrics=as_metrics(live))
    record = load_run(path)
    assert analyse(record.trace, record.spec) == live
    assert record.metrics == json.loads(json.dumps(as_metrics(live)))


@pytest.mark.parametrize("spec", [STEP, SINE], ids=["step", "stepped sine"])
def test_metrics_are_plain_json_with_a_reason_for_every_missing_value(spec):
    metrics = as_metrics(analyse(completed(spec, seed=2), spec))
    text = json.dumps(metrics, allow_nan=False)  # no NaN or infinity anywhere
    assert json.loads(text) == metrics
    assert metrics["analysis"] == (STEP_RESPONSE if spec is STEP else FREQUENCY_RESPONSE)
    items = metrics["steps"] if spec is STEP else metrics["points"]
    missing = 0
    for item in items:
        for name, value in item.items():
            if value is None:
                missing += 1
                assert item["reasons"][name], name
        for name in item["reasons"]:
            assert item[name] is None
    assert missing > 0  # the 1 deg step and the 15 Hz point are not measurable
    assert set(metrics) >= {"quality", "warnings", "definitions"}


def test_every_metric_name_carries_its_unit():
    metrics = as_metrics(analyse(completed(STEP, **NOISELESS), STEP))
    plain_words = {"segment", "label", "reasons", "warnings", "uncertainties", "timing", "samples"}
    ratios = {"step_ratio"}
    units = ("_s", "_rad", "_percent", "_hz")
    for name in metrics["steps"][0]:
        assert name in plain_words | ratios or name.endswith(units), name
    for name in metrics["quality"]["timing"]:
        assert name == "samples" or name.endswith(units), name


def test_the_sampling_achieved_is_reported_for_the_trace_and_each_window():
    step_analysis = analyse(completed(STEP, **NOISELESS), STEP)
    sine_analysis = analyse(completed(SINE, **NOISELESS), SINE)
    # Without jitter, one read per 5 ms bus period during a step...
    step = step_analysis.steps[0]
    assert step.timing.interval_median_s == pytest.approx(0.005)
    assert step.timing.sample_rate_hz == pytest.approx(200, rel=0.02)
    # ...and a command and a read per loop during a sine: half the rate.
    point = sine_analysis.points[0]
    assert point.timing.interval_median_s == pytest.approx(0.010)
    assert point.samples_per_cycle == pytest.approx(100, rel=0.02)
    assert sine_analysis.quality.timing.jitter_s > 0  # the mix of 5 and 10 ms intervals
    jittery = analyse(completed(STEP, seed=0), STEP)
    assert jittery.steps[0].timing.jitter_s > step.timing.jitter_s


def test_missed_replies_are_counted_and_show_as_gaps():
    spec = Step(0.0, (10 * DEG,), dwell_s=1.0, settle_s=0.5)
    result = run(spec, faults=("dropped_replies",), seed=3)
    quality = analyse(result.trace, spec).quality
    during = [m for m in result.trace.missed if m.t_s >= result.trace.t_start_s]
    assert quality.missed_reads == len(during) > 0
    assert quality.timing.interval_max_s > 3 * quality.timing.interval_median_s


def test_a_hold_has_no_response_analysis():
    with pytest.raises(TypeError, match="no response analysis for a hold"):
        analyse(completed(Hold(0.0, 1.0)), Hold(0.0, 1.0))


def test_a_result_cannot_leave_out_a_value_without_a_reason():
    step = analyse(completed(STEP, **NOISELESS), STEP).steps[0]
    with pytest.raises(ValueError, match="no reason"):
        dataclasses.replace(step, delay_s=None)
    with pytest.raises(ValueError, match="a value and a reason"):
        dataclasses.replace(step, reasons={"delay_s": "because"})
    with pytest.raises(ValueError, match="not finite"):
        dataclasses.replace(step, overshoot_percent=math.nan)
    assert isinstance(step, StepResponse)


def test_timing_of_too_few_samples_has_no_rate():
    timing = timing_of([])
    assert timing.samples == 0 and timing.sample_rate_hz is None and timing.jitter_s is None
