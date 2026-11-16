"""The check command: is this actuator alive and set up correctly?

``run_check(bench)`` decides each item below and returns a ``CommandResult``
whose metrics hold every item's status, what was measured and what was
expected. Every check writes a run record (``servobench.records``).

How it reaches the actuator. The read-only items read from the adapter
directly, as the runner's own preflight does: ``describe``, ``read`` and
``read_faults``, nothing that changes the actuator's state. Everything that
moves it goes through ``run_experiment`` and so through validation, rehearsal,
the safety monitor and the safe state. The actuator is moved only if every
read-only item that says it is safe to move (``GATES``) has passed.

Item statuses:

- ``pass``: measured, and as it should be.
- ``fail``: measured, and wrong. Its ``next`` says what to do.
- ``warning``: only for ``protections``, which is a declaration, not a
  measurement: a protection the device does not have, or may not have.
- ``skipped``: not attempted, with the reason: ``--no-motion``, or an earlier
  item failed. Never counted as a pass.
- ``unknown``: attempted but could not be decided, with the reason.

The items:

``responds``
    The adapter answers ``describe``, a read of every signal it declares
    (each a finite number) and a fault poll, allowing ``missed_replies_max``
    missed replies in a row. Fail otherwise; every other item is then skipped.
``identity``
    The adapter's identity against the bench file's ``[target]``: the adapter
    name, and the device id when the bench file gives one. Unknown when the
    bench file gives an id and the device reports none.
``faults``
    The device reports no fault, before and (with motion) after the moves.
``voltage``
    The median supply voltage over the read-only readings is inside
    ``voltage_min`` to ``voltage_max``. Unknown if the adapter cannot read it.
``temperature``
    The median temperature over the read-only readings is at most
    ``start_temperature_max``, the warmest an experiment may start at.
``bus``
    ``BUS_TEST_READS`` back-to-back reads of every signal: at least
    ``BUS_REPLY_SHARE_MIN`` of them answered, and the answered reads came at
    ``[check] sample_rate_min`` or faster, counting the time lost to missed
    ones. With motion, the reads during the moves must also have reached it.
``protections``
    The protections the adapter declares, each yes, no or unknown. Pass only
    if every one is yes; otherwise a warning, never a fail, because a missing
    protection is a fact about the hardware that no setting fixes, and a check
    that can never pass on such hardware would teach its users to ignore it.
    The warning names what is missing so that the reader knows what only the
    host-side monitor and the power supply guard.
``zero``
    With torque off and a pendulum load, the arm hangs straight down, which
    the model and the bench limits take as position zero. The median position
    over the read-only readings, and with motion also the analysis's
    ``position_before_enable_rad``, must be within ``[check] zero_tolerance``
    of zero. Unknown without a ``[load]``: nothing then pulls the output to a
    known position. The item assumes the actuator was at rest with torque off
    when check started, as the runner leaves it after every run.
``direction`` (moves the actuator)
    A ``Step`` of ``[check] move`` up from where the actuator rests, and back.
    Each step must change the position the commanded way by at least
    ``DIRECTION_RATIO_MIN`` of the command (the analysis's ``step_ratio``).
    Fail if a step moved the opposite way or less than that; unknown if the
    analysis could not fix a step's positions.
``temperature_rise`` (moves the actuator)
    The temperature at the end of the moves (mean over the last
    ``RISE_WINDOW_S``) minus the reading just before enable, at most
    ``[check] temperature_rise_max``.

Overall: ``judge_run`` first, when the moves did not complete; then
``responds`` failed: error (exit 4); ``faults`` failed: fail (exit 4); any
other failure: fail (exit 1); anything skipped or unknown: partial (exit 0);
otherwise pass (exit 0), with any warning in the summary.

The cool-down across invocations. Before the moves, the newest record under
``runs/`` of a run that enabled this actuator (same adapter name and device
id) gives the time since that run ended (``ended_utc``), which is handed to the
runner with ``note_previous_run``. With no such record, only
``start_temperature_max`` applies. Records of other actuators are ignored.
"""

from __future__ import annotations

import json
import math
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from servobench import records
from servobench.adapters import UnknownAdapter, open_adapter
from servobench.analysis import StepAnalysis, analyse_step, as_metrics, timing_of
from servobench.clock import utc_now
from servobench.commands import (
    ERROR,
    FAIL,
    PARTIAL,
    PASS,
    SAFE_STATE_NOT_CONFIRMED,
    CommandResult,
    ExitCode,
    RunJudgement,
    error_details,
    judge_run,
)
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
    Support,
)
from servobench.experiments.runner import Outcome, RunResult, note_previous_run, run_experiment
from servobench.experiments.spec import Step, command_band_rad
from servobench.limits import WAIT, Bench
from servobench.units import Dimension, format_quantity

COMMAND = "check"

# Item statuses.
ITEM_PASS = "pass"
ITEM_FAIL = "fail"
ITEM_WARNING = "warning"
ITEM_SKIPPED = "skipped"
ITEM_UNKNOWN = "unknown"
STATUSES = (ITEM_PASS, ITEM_FAIL, ITEM_WARNING, ITEM_SKIPPED, ITEM_UNKNOWN)

RESPONDS = "responds"
IDENTITY = "identity"
FAULTS = "faults"
VOLTAGE_ITEM = "voltage"
TEMPERATURE_ITEM = "temperature"
BUS = "bus"
PROTECTIONS = "protections"
ZERO = "zero"
DIRECTION = "direction"
TEMPERATURE_RISE = "temperature_rise"
ITEMS = (
    RESPONDS,
    IDENTITY,
    FAULTS,
    VOLTAGE_ITEM,
    TEMPERATURE_ITEM,
    BUS,
    PROTECTIONS,
    ZERO,
    DIRECTION,
    TEMPERATURE_RISE,
)
# The items that move the actuator. --no-motion skips them.
MOTION_ITEMS = (DIRECTION, TEMPERATURE_RISE)
# The read-only items that must pass before check moves the actuator: it must
# be the right device, unfaulted, inside its supply and temperature range, on
# a bus reliable enough for the safety monitor to watch every sample.
GATES = (RESPONDS, IDENTITY, FAULTS, VOLTAGE_ITEM, TEMPERATURE_ITEM, BUS)

# The bus test: this many back-to-back reads, of which this share must be answered.
# 200 reads are one second at the simulated servo's 200 Hz; 99 % allows two
# missed replies, which a sound serial link does not have.
BUS_TEST_READS = 200
BUS_REPLY_SHARE_MIN = 0.99
# The moves: settle at the start, then the step up and the step back, each held
# this long. Long enough for the step analysis to fix the positions before and
# after each step (it uses the last 20 % of each).
CHECK_SETTLE_S = 0.5
CHECK_DWELL_S = 1.0
# A step must change the position the commanded way by at least this share of
# the command. The step analysis flags the same share as suspicious; below it,
# a positive reading could be noise, a stall or a stuck sensor.
DIRECTION_RATIO_MIN = 0.5
# The temperature at the end of the moves is the mean over this last stretch.
RISE_WINDOW_S = 0.25

# The protections a device can declare (contract.Capabilities), in plain words.
PROTECTION_NAMES: dict[str, str] = {
    "position_limit": "position limit",
    "speed_limit": "speed limit",
    "torque_limit": "torque or current limit",
    "temperature_cutoff": "temperature cutoff",
    "voltage_cutoff": "voltage cutoff",
    "safe_state_on_comm_loss": "safe state on lost communication",
    "fault_latching": "fault latching",
}
FEATURE_NAMES = ("onboard_capture", "onboard_test_signals")


@dataclass
class Item:
    """One thing check decides."""

    name: str
    status: str
    detail: str  # what was found, in a sentence
    values: dict[str, Any] = field(default_factory=dict)  # SI, names with their unit
    next: str | None = None  # what to do, for a failure, a warning or an unknown
    human: bool = True  # whether a failure needs a human; False if waiting is enough

    def to_json(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "detail": self.detail,
            "values": self.values,
            "next": self.next,
        }


def run_check(
    bench: Bench,
    *,
    motion: bool = True,
    runs_dir: str | Path = records.RUNS_DIR,
    adapter: Adapter | None = None,
) -> CommandResult:
    """Check the bench file's actuator; ``motion=False`` for read-only.

    ``adapter`` is opened from the bench file (``servobench.adapters``) when not
    given. Every outcome is returned as a result, an unexpected exception
    included; only an unknown adapter name is raised (``UnknownAdapter``), as a
    usage error with nothing contacted and nothing to record.
    """
    check = _Check(bench, motion, Path(runs_dir))
    opened = adapter is None
    try:
        if adapter is None:
            try:
                adapter = open_adapter(bench)
            except AdapterError as error:
                check.no_response(f"the actuator could not be reached: {_error(error)}")
                return check.finish()
        try:
            check.read_only(adapter)
            if not motion:
                check.skip_motion()
            elif check.may_move():
                check.move(adapter)
        finally:
            if opened:
                adapter.close()  # releases the connection; does not change the actuator
        return check.finish()
    except UnknownAdapter:
        raise
    except Exception as error:  # noqa: BLE001 - reported in full in the result
        return check.finish(error)


class _Check:
    def __init__(self, bench: Bench, motion: bool, runs_dir: Path) -> None:
        self.bench = bench
        self.motion = motion
        self.runs_dir = runs_dir
        self.items: dict[str, Item] = {}
        self.description: Description | None = None
        self.readings: list[Sample] = []
        self.faults_before: tuple[Fault, ...] | None = None
        self.at_rest_position_rad: float | None = None
        self.notes: list[str] = []
        self.cooldown: dict[str, Any] | None = None
        self.result: RunResult | None = None
        self.judgement: RunJudgement | None = None
        self.analysis: StepAnalysis | None = None
        self.ended_utc = None

    def set(self, item: Item) -> None:
        self.items[item.name] = item

    # -- the read-only items -------------------------------------------------------

    def no_response(self, why: str) -> None:
        self.set(
            Item(
                RESPONDS,
                ITEM_FAIL,
                f"The actuator did not respond: {why}.",
                next=(
                    "Nothing was moved. Tell the human: check that the actuator is powered and"
                    " connected, and that bench.toml's [target] names the right adapter, port"
                    " and id."
                ),
            )
        )
        self.skip_rest("not checked, because the actuator did not respond")

    def skip_rest(self, why: str) -> None:
        for name in ITEMS:
            if name not in self.items:
                self.set(Item(name, ITEM_SKIPPED, why[0].upper() + why[1:] + "."))

    def read_only(self, adapter: Adapter) -> None:
        try:
            description = adapter.describe()
            self.description = description
            first = self.read_retrying(adapter.read)
            faults = self.read_retrying(adapter.read_faults)
        except AdapterError as error:
            self.no_response(_error(error))
            return
        self.faults_before = faults
        bad = {name: value for name, value in first.values.items() if not _finite(value)}
        if bad:
            listed = ", ".join(f"{name} = {value!r}" for name, value in bad.items())
            self.no_response(f"it replied, but its reading was not usable ({listed})")
            return
        self.readings.append(first)
        self.set(
            Item(
                RESPONDS,
                ITEM_PASS,
                f"The actuator answered describe, a read of its {len(first.values)} signals and"
                " a fault poll.",
                {"signals": list(description.signal_names)},
            )
        )
        self.set(self.identity_item(description))
        self.set(self.faults_item(faults))
        bus_samples, missed, elapsed_s, last_error = self.bus_test(adapter)
        self.readings += bus_samples
        self.set(self.voltage_item(description))
        self.set(self.temperature_item(description))
        self.set(self.bus_item(description, bus_samples, missed, elapsed_s, last_error))
        self.set(self.protections_item(description))
        self.set(self.zero_item())

    def read_retrying(self, call):
        """Call a read, allowing missed_replies_max missed replies in a row, as the runner does."""
        allowed = self.bench.runner.missed_replies_max
        for attempt in range(allowed + 1):
            try:
                return call()
            except CommunicationError:
                if attempt == allowed:
                    raise
        raise AssertionError("unreachable")

    def identity_item(self, description: Description) -> Item:
        identity = description.identity
        target = self.bench.target
        values = {
            "adapter": identity.adapter,
            "model": identity.model,
            "device_id": identity.device_id,
            "firmware": identity.firmware,
            "simulated": identity.simulated,
            "bench_adapter": target.adapter,
            "bench_id": target.id,
            "bench_port": target.port,
        }
        found = (
            f"{identity.model}, id {identity.device_id or 'none'}, firmware"
            f" {identity.firmware or 'unknown'}"
        )
        problems = []
        if identity.adapter != target.adapter:
            problems.append(
                f"the adapter calls itself {identity.adapter!r}, but bench.toml names"
                f" {target.adapter!r}"
            )
        if target.id is not None:
            if identity.device_id is None:
                return Item(
                    IDENTITY,
                    ITEM_UNKNOWN,
                    f"bench.toml names id {target.id}, but the device ({found}) reports no id,"
                    " so which device answered cannot be confirmed.",
                    values,
                    next="Tell the human, who can confirm by hand which device is connected.",
                )
            if identity.device_id != str(target.id):
                problems.append(
                    f"the device that answered has id {identity.device_id}, but bench.toml names"
                    f" id {target.id}"
                )
        if problems:
            return Item(
                IDENTITY,
                ITEM_FAIL,
                f"Not the actuator bench.toml describes: {'; '.join(problems)}.",
                values,
                next=(
                    "Nothing was moved. Tell the human: either another actuator is connected,"
                    " or bench.toml's [target] is wrong, and only its owner changes it."
                ),
            )
        scope = (
            "" if target.id is not None else "; bench.toml names no id, so only the adapter was"
            " compared"
        )
        simulated = " (simulated)" if identity.simulated else ""
        return Item(
            IDENTITY, ITEM_PASS, f"{found}{simulated}, as bench.toml says{scope}.", values
        )

    def faults_item(self, faults: tuple[Fault, ...], after: bool = False) -> Item:
        values = {"faults": [{"code": f.code, "description": f.description} for f in faults]}
        if not faults:
            return Item(FAULTS, ITEM_PASS, "The device reports no fault.", values)
        listed = "; ".join(f"{fault.code}: {fault.description}" for fault in faults)
        when = "after check's moves" if after else "before anything was moved"
        return Item(
            FAULTS,
            ITEM_FAIL,
            f"The device reports a fault {when} ({listed}).",
            values,
            next=(
                "The tool cannot clear faults, and must not try. Tell the human: they must find"
                " the cause and clear the fault (often by power-cycling the actuator), then run"
                " check again."
            ),
        )

    def bus_test(self, adapter: Adapter) -> tuple[list[Sample], int, float, str | None]:
        samples: list[Sample] = []
        missed = 0
        last_error = None
        start_s = adapter.clock.now_s()
        for _ in range(BUS_TEST_READS):
            try:
                samples.append(adapter.read())
            except CommunicationError as error:
                missed += 1
                last_error = _error(error)
        return samples, missed, adapter.clock.now_s() - start_s, last_error

    def voltage_item(self, description: Description) -> Item:
        limits = self.bench.limits
        low, high = limits.voltage_min_v, limits.voltage_max_v
        if VOLTAGE not in description.signal_names:
            return Item(
                VOLTAGE_ITEM,
                ITEM_UNKNOWN,
                "The adapter cannot read the supply voltage, so it could not be checked.",
                {"voltage_min_v": low, "voltage_max_v": high},
                next="Tell the human to check the supply voltage with a meter.",
            )
        stats = _stats(self.readings, VOLTAGE)
        values = {
            "voltage_v": stats["median"],
            "voltage_lowest_v": stats["lowest"],
            "voltage_highest_v": stats["highest"],
            "readings": stats["count"],
            "voltage_min_v": low,
            "voltage_max_v": high,
        }
        if stats["bad"]:
            return Item(
                VOLTAGE_ITEM,
                ITEM_FAIL,
                f"{stats['bad']} voltage readings were not numbers.",
                values,
                next="Tell the human: the adapter returns unusable voltage readings.",
            )
        median = stats["median"]
        if low <= median <= high:
            return Item(
                VOLTAGE_ITEM,
                ITEM_PASS,
                f"Supply {_v(median)}, inside {_v(low)} to {_v(high)}.",
                values,
            )
        return Item(
            VOLTAGE_ITEM,
            ITEM_FAIL,
            f"Supply {_v(median)}, outside voltage_min {_v(low)} to voltage_max {_v(high)}.",
            values,
            next=(
                "Nothing was moved. Tell the human: check the power supply's setting, its"
                " current limit and the wiring."
            ),
        )

    def temperature_item(self, description: Description) -> Item:
        limits = self.bench.limits
        start_max = limits.start_temperature_max_degc
        if TEMPERATURE not in description.signal_names:
            return Item(
                TEMPERATURE_ITEM,
                ITEM_UNKNOWN,
                "The adapter cannot read the temperature, so it could not be checked.",
                {"start_temperature_max_degc": start_max},
                next="Tell the human: without a temperature reading nothing can be run safely.",
            )
        stats = _stats(self.readings, TEMPERATURE)
        values = {
            "temperature_degc": stats["median"],
            "readings": stats["count"],
            "start_temperature_max_degc": start_max,
            "temperature_max_degc": limits.temperature_max_degc,
        }
        if stats["bad"]:
            return Item(
                TEMPERATURE_ITEM,
                ITEM_FAIL,
                f"{stats['bad']} temperature readings were not numbers.",
                values,
                next="Tell the human: the adapter returns unusable temperature readings.",
            )
        median = stats["median"]
        if median <= start_max:
            return Item(
                TEMPERATURE_ITEM,
                ITEM_PASS,
                f"{_degc(median)}, at most start_temperature_max {_degc(start_max)}.",
                values,
            )
        hot = median > limits.temperature_max_degc
        return Item(
            TEMPERATURE_ITEM,
            ITEM_FAIL,
            f"{_degc(median)}, above start_temperature_max {_degc(start_max)}"
            + (f" and above temperature_max {_degc(limits.temperature_max_degc)}" if hot else "")
            + ".",
            values,
            next=(
                "Nothing was moved. Tell the human: the actuator is hotter than any experiment"
                " may run at; find out why before running anything."
                if hot
                else "Nothing was moved. Let the actuator cool, with torque off, and run check"
                " again."
            ),
            human=hot,
        )

    def bus_item(
        self,
        description: Description,
        samples: list[Sample],
        missed: int,
        elapsed_s: float,
        last_error: str | None,
    ) -> Item:
        required_hz = self.bench.check.sample_rate_min_hz
        declared_hz = description.max_sample_rate_hz
        answered = len(samples)
        rate_hz = answered / elapsed_s if elapsed_s > 0 else None
        timing = timing_of([sample.t_s for sample in samples])
        values = {
            "reads": BUS_TEST_READS,
            "answered": answered,
            "missed": missed,
            "answered_rate_hz": rate_hz,
            "interval_median_s": timing.interval_median_s,
            "interval_max_s": timing.interval_max_s,
            "jitter_s": timing.jitter_s,
            "declared_max_sample_rate_hz": declared_hz,
            "sample_rate_min_hz": required_hz,
            "last_error": last_error,
        }
        problems = []
        if answered < BUS_REPLY_SHARE_MIN * BUS_TEST_READS:
            problems.append(
                f"{missed} of {BUS_TEST_READS} back-to-back reads got no reply (at most"
                f" {BUS_TEST_READS - math.ceil(BUS_REPLY_SHARE_MIN * BUS_TEST_READS)} may)"
            )
        if rate_hz is None or rate_hz < required_hz:
            achieved = "no" if rate_hz is None else _hz(rate_hz)
            problems.append(
                f"answered reads came at {achieved} rate, below sample_rate_min {_hz(required_hz)}"
            )
        if declared_hz < required_hz:
            problems.append(
                f"the adapter declares at most {_hz(declared_hz)}, below sample_rate_min"
                f" {_hz(required_hz)}"
            )
        if problems:
            return Item(
                BUS,
                ITEM_FAIL,
                f"The bus is not reliable enough: {'; '.join(problems)}.",
                values,
                next=(
                    "Nothing was moved: the safety monitor needs every reading, so check does not"
                    " move an actuator on a bus that drops replies or runs slow. Tell the human:"
                    " check the cable, the connectors, the baud rate and the power."
                ),
            )
        return Item(
            BUS,
            ITEM_PASS,
            f"{answered} of {BUS_TEST_READS} back-to-back reads answered, at {_hz(rate_hz)}"
            f" (sample_rate_min {_hz(required_hz)}).",
            values,
        )

    def protections_item(self, description: Description) -> Item:
        capabilities = description.capabilities
        declared = {name: getattr(capabilities, name).value for name in PROTECTION_NAMES}
        values = {
            "protections": declared,
            "comm_loss_timeout_s": capabilities.comm_loss_timeout_s,
            "features": {name: getattr(capabilities, name).value for name in FEATURE_NAMES},
        }
        missing = [
            PROTECTION_NAMES[name] for name, value in declared.items() if value == Support.NO.value
        ]
        unknown = [
            PROTECTION_NAMES[name]
            for name, value in declared.items()
            if value == Support.UNKNOWN.value
        ]
        if not missing and not unknown:
            return Item(
                PROTECTIONS,
                ITEM_PASS,
                f"The adapter declares all {len(declared)} protections on the device.",
                values,
            )
        parts = []
        if missing:
            parts.append(f"the device has no {_or(missing)} of its own")
        if unknown:
            parts.append(f"it is not known whether it has a {_or(unknown)}")
        detail = "; ".join(parts) + ", as its adapter declares."
        return Item(
            PROTECTIONS,
            ITEM_WARNING,
            detail[0].upper() + detail[1:],
            values,
            next=(
                "Nothing for check to fix: this is what the hardware has, and the tool treats"
                " an unknown protection as missing. Against those hazards nothing acts except"
                " the tool's safety monitor, which watches only while a command runs, the bench"
                " power supply's current limit and the human's hand on the power."
            ),
        )

    def zero_item(self) -> Item:
        tolerance_rad = self.bench.check.zero_tolerance_rad
        stats = _stats(self.readings, POSITION)
        position_rad = stats["median"]
        self.at_rest_position_rad = position_rad
        load = self.bench.load
        values = {
            "position_at_rest_rad": position_rad,
            "expected_rad": 0.0,
            "zero_tolerance_rad": tolerance_rad,
            "load": None
            if load is None
            else {"mass_kg": load.mass_kg, "length_m": load.length_m},
        }
        if load is None:
            return Item(
                ZERO,
                ITEM_UNKNOWN,
                f"At rest the actuator reads {_deg(position_rad)}, but bench.toml has no [load],"
                " so nothing pulls the output to a known position and the zero cannot be judged.",
                values,
                next=(
                    "To have the zero checked, the owner fits the pendulum arm and describes it"
                    " in bench.toml's [load]."
                ),
            )
        return self.judge_zero(values)

    def judge_zero(self, values: dict[str, Any]) -> Item:
        tolerance_rad = values["zero_tolerance_rad"]
        readings = {"at rest": values["position_at_rest_rad"]}
        if values.get("position_before_enable_rad") is not None:
            readings["just before the moves"] = values["position_before_enable_rad"]
        off = {where: value for where, value in readings.items() if abs(value) > tolerance_rad}
        if not off:
            shown = ", ".join(f"{_deg(value)} {where}" for where, value in readings.items())
            return Item(
                ZERO,
                ITEM_PASS,
                f"Reads {shown}, within zero_tolerance {_deg(tolerance_rad)} of zero, where the"
                " arm hangs.",
                values,
            )
        where, value = next(iter(off.items()))
        return Item(
            ZERO,
            ITEM_FAIL,
            f"Reads {_deg(value)} {where}, more than zero_tolerance {_deg(tolerance_rad)} from"
            " zero, where the arm should hang.",
            values,
            next=(
                "Tell the human: either the actuator's zero offset is wrong, or the arm is not"
                " mounted to hang straight down at position zero, which the model assumes."
                " Until it is fixed, the travel limits in bench.toml are measured from the"
                " wrong place. A zero offset is usually stored persistently on the device;"
                " only the owner may change it."
            ),
        )

    # -- the moves -----------------------------------------------------------------------

    def skip_motion(self) -> None:
        for name in MOTION_ITEMS:
            if name not in self.items:  # an earlier failure may already have skipped it
                self.set(
                    Item(
                        name,
                        ITEM_SKIPPED,
                        "Skipped (--no-motion): checking it moves the actuator. It has not been"
                        " checked, and it is not a pass.",
                    )
                )

    def may_move(self) -> bool:
        blocking = [name for name in GATES if self.items[name].status != ITEM_PASS]
        if blocking:
            for name in MOTION_ITEMS:
                self.set(
                    Item(
                        name,
                        ITEM_SKIPPED,
                        f"Not checked: check moves the actuator only once {_and(list(GATES))}"
                        f" have passed, and {_and(blocking)} did not.",
                    )
                )
            return False
        return True

    def move(self, adapter: Adapter) -> None:
        bench = self.bench
        move_rad = bench.check.move_rad
        low_rad, high_rad = command_band_rad(bench.limits)
        centre_rad = min(max(self.at_rest_position_rad, low_rad), high_rad - move_rad)
        spec = Step(
            centre_rad=centre_rad,
            amplitudes_rad=(move_rad,),
            dwell_s=CHECK_DWELL_S,
            settle_s=CHECK_SETTLE_S,
        )
        self.cooldown = self.previous_run(adapter)
        result = run_experiment(adapter, bench, spec)
        self.ended_utc = utc_now()
        self.result = result
        self.judgement = judge_run(result)
        if result.faults_after:
            self.set(self.faults_item(result.faults_after, after=True))
        if self.judgement is not None:
            for name in MOTION_ITEMS:
                self.set(
                    Item(
                        name,
                        ITEM_UNKNOWN,
                        f"Not judged, because the moves did not end normally: {result.summary}",
                    )
                )
            return
        self.analysis = analyse_step(result.trace, spec)
        self.set(self.direction_item(self.analysis))
        self.set(self.temperature_rise_item(result))
        zero = self.items[ZERO]
        if zero.status != ITEM_UNKNOWN:
            values = dict(zero.values)
            values["position_before_enable_rad"] = self.analysis.quality.position_before_enable_rad
            self.set(self.judge_zero(values))
        self.add_run_rate(self.analysis)

    def previous_run(self, adapter: Adapter) -> dict[str, Any]:
        identity = self.description.identity
        cooldown_s = self.bench.limits.cooldown_s
        last = records.latest_enabled_run(identity.adapter, identity.device_id, self.runs_dir)
        if last is None:
            self.notes.append(
                f"No earlier run that enabled this actuator is recorded under {self.runs_dir};"
                " no cool-down applies, only start_temperature_max."
            )
            return {"previous_run": None, "cooldown_s": cooldown_s, "rest_left_s": 0.0}
        ago_s = (utc_now() - last.ended_utc).total_seconds()
        note_previous_run(adapter, ago_s)
        rest_left_s = max(0.0, cooldown_s - max(0.0, ago_s))
        if rest_left_s > 0 and identity.simulated and self.bench.runner.when_too_warm == WAIT:
            self.notes.append(
                f"The cool-down after {last.name} had {_s(rest_left_s)} left. The simulated servo"
                " waits on its own virtual clock, so no real time passed."
            )
        return {
            "previous_run": str(last.path),
            "previous_run_ended_utc": last.ended_utc.isoformat(),
            "ended_s_ago": ago_s,
            "cooldown_s": cooldown_s,
            "rest_left_s": rest_left_s,
        }

    def direction_item(self, analysis: StepAnalysis) -> Item:
        steps = []
        reversed_, short, undecided = [], [], []
        for step in analysis.steps:
            steps.append(
                {
                    "label": step.label,
                    "commanded_step_rad": step.commanded_step_rad,
                    "measured_step_rad": step.measured_step_rad,
                    "step_ratio": step.step_ratio,
                }
            )
            ratio = step.step_ratio
            if ratio is None:
                undecided.append((step, step.reasons.get("step_ratio", "not measured")))
            elif ratio <= -DIRECTION_RATIO_MIN:
                reversed_.append(step)
            elif ratio < DIRECTION_RATIO_MIN:
                short.append(step)
        values = {"steps": steps, "direction_ratio_min": DIRECTION_RATIO_MIN}
        if reversed_:
            step = reversed_[0]
            return Item(
                DIRECTION,
                ITEM_FAIL,
                f"Commanded {_deg_signed(step.commanded_step_rad)}, the position changed by"
                f" {_deg_signed(step.measured_step_rad)}: the actuator turns the opposite way"
                " to its commands.",
                values,
                next=(
                    "Tell the human: the actuator's direction setting or its wiring is reversed."
                    " A direction setting is usually stored persistently on the device; only the"
                    " owner may change it. Run nothing else on this actuator until it is fixed."
                ),
            )
        if short:
            step = short[0]
            if step.measured_step_rad == 0:
                found = "the position reading did not change at all"
            else:
                found = (
                    f"the position changed by only {_deg_signed(step.measured_step_rad)}"
                    f" ({step.step_ratio:.0%} of it)"
                )
            return Item(
                DIRECTION,
                ITEM_FAIL,
                f"Commanded {_deg_signed(step.commanded_step_rad)}, {found}.",
                values,
                next=(
                    "Tell the human: the output may be jammed or disconnected from the motor, too"
                    " weak for the load, or its position reading may be stuck."
                ),
            )
        if undecided:
            step, reason = undecided[0]
            return Item(
                DIRECTION,
                ITEM_UNKNOWN,
                f"The direction could not be decided from '{step.label}': {reason}.",
                values,
                next="Run check again; if it stays unknown, tell the human.",
            )
        shown = "; ".join(
            f"{_deg_signed(step.commanded_step_rad)} commanded,"
            f" {_deg_signed(step.measured_step_rad)} measured"
            for step in analysis.steps
        )
        return Item(
            DIRECTION,
            ITEM_PASS,
            f"Each move went the commanded way ({shown}).",
            values,
        )

    def temperature_rise_item(self, result: RunResult) -> Item:
        trace = result.trace
        limit_degc = self.bench.check.temperature_rise_max_degc
        temperatures = [
            (sample.t_s, sample.values[TEMPERATURE])
            for sample in trace.samples
            if TEMPERATURE in sample.values
        ]
        values: dict[str, Any] = {"temperature_rise_max_degc": limit_degc}
        if len(temperatures) < 2 or trace.t_start_s is None:
            return Item(
                TEMPERATURE_RISE,
                ITEM_UNKNOWN,
                "Too few temperature readings during the moves to tell.",
                values,
            )
        before_degc = temperatures[0][1]  # the reading just before enable
        end_s = temperatures[-1][0]
        last = [value for t_s, value in temperatures if t_s >= end_s - RISE_WINDOW_S]
        after_degc = statistics.fmean(last)
        rise_degc = after_degc - before_degc
        duration_s = end_s - trace.t_start_s
        values.update(
            {
                "temperature_before_degc": before_degc,
                "temperature_after_degc": after_degc,
                "temperature_rise_degc": rise_degc,
                "duration_s": duration_s,
            }
        )
        found = (
            f"The temperature changed by {_degc_signed(rise_degc)} in {_s(duration_s)} of small"
            f" moves ({_degc(before_degc)} to {_degc(after_degc)})"
        )
        if rise_degc <= limit_degc:
            return Item(
                TEMPERATURE_RISE,
                ITEM_PASS,
                f"{found}, at most temperature_rise_max {_degc(limit_degc)}.",
                values,
            )
        return Item(
            TEMPERATURE_RISE,
            ITEM_FAIL,
            f"{found}, more than temperature_rise_max {_degc(limit_degc)}: it heats far faster"
            " than small moves explain.",
            values,
            next=(
                "Tell the human: something is loading the actuator, or its cooling or its"
                " temperature sensor is wrong. Let it cool, with torque off, and run nothing"
                " longer until the cause is known."
            ),
        )

    def add_run_rate(self, analysis: StepAnalysis) -> None:
        bus = self.items[BUS]
        quality = analysis.quality
        values = dict(bus.values)
        rate_hz = quality.timing.sample_rate_hz
        values["during_moves_rate_hz"] = rate_hz
        values["during_moves_missed"] = quality.missed_reads
        required_hz = self.bench.check.sample_rate_min_hz
        if rate_hz is None or rate_hz < required_hz:
            achieved = "no" if rate_hz is None else _hz(rate_hz)
            self.set(
                Item(
                    BUS,
                    ITEM_FAIL,
                    f"During the moves the readings came at {achieved} rate, below"
                    f" sample_rate_min {_hz(required_hz)}.",
                    values,
                    next=(
                        "Tell the human: the bus cannot keep up while commands are also sent;"
                        " check the baud rate and the cable."
                    ),
                )
            )
            return
        bus.values = values
        bus.detail += f" During the moves: {_hz(rate_hz)}, {quality.missed_reads} missed."

    # -- the verdict ---------------------------------------------------------------------

    def finish(self, error: BaseException | None = None) -> CommandResult:
        for name in ITEMS:
            if name not in self.items:
                self.set(Item(name, ITEM_UNKNOWN, "Not decided: check stopped with an error."))
        items = [self.items[name] for name in ITEMS]
        by_status = {status: [i.name for i in items if i.status == status] for status in STATUSES}
        failed = [item for item in items if item.status == ITEM_FAIL]
        warnings = [item for item in items if item.status == ITEM_WARNING]
        not_measured = [item for item in items if item.status in (ITEM_SKIPPED, ITEM_UNKNOWN)]
        judgement = self.judgement
        result = self.result

        if error is not None:
            verdict, exit_code, needs_human = ERROR, ExitCode.DEVICE, True
            summary = f"Error: check failed with an unexpected {type(error).__name__}: {error}."
            if result is None:
                state = "check had not enabled the actuator; it had only read from it."
            else:
                state = f"Before the error, the experiment runner reported: {result.summary}"
            next_ = (
                "This is a fault in servobench, not a finding about the actuator. Tell the human"
                f" and give them the traceback under error. {state}"
            )
            if result is not None and result.outcome is Outcome.SAFE_STATE_NOT_CONFIRMED:
                verdict = SAFE_STATE_NOT_CONFIRMED
                summary = f"{result.summary} Then check failed: {type(error).__name__}: {error}."
                next_ = f"{judgement.next} {next_}"
        elif judgement is not None:
            verdict, exit_code = judgement.verdict, judgement.exit_code
            needs_human, next_ = judgement.needs_human, judgement.next
            summary = result.summary
        elif self.items[RESPONDS].status == ITEM_FAIL:
            item = self.items[RESPONDS]
            verdict, exit_code, needs_human, next_ = ERROR, ExitCode.DEVICE, True, item.next
            summary = f"Error: {item.detail}"
        elif failed:
            verdict = FAIL
            device_fault = self.items[FAULTS].status == ITEM_FAIL
            exit_code = ExitCode.DEVICE if device_fault else ExitCode.FAIL
            needs_human = any(item.human for item in failed)
            if len(failed) == 1:
                summary = f"Fail: {failed[0].name}: {failed[0].detail}"
            else:
                listed = "; ".join(f"{item.name}: {item.detail}" for item in failed)
                summary = f"Fail: {len(failed)} items failed. {listed}"
            next_ = " ".join(f"{item.name}: {item.next}" for item in failed if item.next)
        elif not_measured:
            verdict, exit_code, needs_human = PARTIAL, ExitCode.PASS, False
            summary, next_ = self.partial_summary(items, not_measured)
        else:
            verdict, exit_code, needs_human, next_ = PASS, ExitCode.PASS, False, None
            summary = f"Pass: all {len(items) - len(warnings)} measured items passed."
        if error is None and verdict in (PASS, PARTIAL, FAIL) and warnings:
            summary += " Warning: " + " ".join(item.detail for item in warnings)

        metrics: dict[str, Any] = {
            "items": {item.name: item.to_json() for item in items},
            "by_status": by_status,
            "motion": self.motion_state(),
            "run": self.run_metrics(),
            "cooldown": self.cooldown,
            "notes": self.notes,
            "bench": {"source": self.bench.source, "fingerprint": self.bench.fingerprint},
            "limits": records.plain(self.bench.limits),
            "check_settings": records.plain(self.bench.check),
        }
        command_result = CommandResult(
            command=COMMAND,
            verdict=verdict,
            summary=summary,
            exit_code=exit_code,
            needs_human=needs_human,
            next=next_,
            metrics=metrics,
            error=None if error is None else error_details(error),
        )
        self.write_record(command_result)
        return command_result

    def partial_summary(self, items: list[Item], not_measured: list[Item]) -> tuple[str, str]:
        measured = [item for item in items if item.status == ITEM_PASS]
        skipped = [item.name for item in not_measured if item.status == ITEM_SKIPPED]
        unknown = [item for item in not_measured if item.status == ITEM_UNKNOWN]
        parts = [f"Partial: nothing failed, but not everything was checked. {len(measured)} passed"]
        if skipped:
            why = " (--no-motion)" if not self.motion else ""
            parts.append(f"{_and(skipped)} skipped{why}")
        if unknown:
            parts.append(f"{_and([item.name for item in unknown])} could not be decided")
        summary = "; ".join(parts) + "."
        nexts = []
        if not self.motion:
            simulated = self.description is not None and self.description.identity.simulated
            agree = "" if simulated else ", once the human agrees that the actuator may move"
            nexts.append(f"To check {_and(skipped)}, run check without --no-motion{agree}.")
        nexts += [f"{item.name}: {item.next}" for item in unknown if item.next]
        return summary, " ".join(nexts)

    def motion_state(self) -> str:
        if not self.motion:
            return "skipped: --no-motion"
        if self.result is None:
            return "not run: a read-only item did not pass"
        return f"run: {self.result.outcome.value}"

    def run_metrics(self) -> dict[str, Any] | None:
        result = self.result
        if result is None:
            return None
        stop = result.stop
        return {
            "outcome": result.outcome.value,
            "summary": result.summary,
            "stop_kind": None if stop is None else stop.kind,
            "stop_stage": None if stop is None else stop.stage,
            "stop_message": None if stop is None else stop.message,
            "safe_state": result.safe_state.status.value,
            "cooled_s": result.cooled_s,
            "spec": result.spec.to_dict(),
            "notes": list(result.notes),
        }

    def write_record(self, command_result: CommandResult) -> None:
        metrics = dict(command_result.metrics)
        if command_result.error is not None:
            metrics["error"] = command_result.error
        if self.analysis is not None:
            metrics["step_response"] = as_metrics(self.analysis)
        try:
            if self.result is not None:
                path = records.write_run(
                    self.result,
                    command=COMMAND,
                    runs_dir=self.runs_dir,
                    verdict=command_result.verdict,
                    metrics=metrics,
                    summary=command_result.summary,
                    ended_utc=self.ended_utc,
                    readings=self.readings,
                )
            else:
                path = records.write_readonly_run(
                    command=COMMAND,
                    bench=self.bench,
                    description=self.description,
                    verdict=command_result.verdict,
                    summary=command_result.summary,
                    metrics=metrics,
                    readings=self.readings,
                    faults=self.faults_before,
                    notes=self.notes,
                    runs_dir=self.runs_dir,
                )
        except Exception as error:  # noqa: BLE001 - the result says so
            command_result.summary += (
                f" The run record could not be written: {type(error).__name__}: {error}."
            )
            if command_result.verdict != SAFE_STATE_NOT_CONFIRMED:
                command_result.verdict = ERROR
                command_result.exit_code = ExitCode.DEVICE
            command_result.needs_human = True
            command_result.next = (
                f"{command_result.next or ''} Tell the human that no record was kept: without it"
                " the next run cannot know when this one ended, for the cool-down."
            ).strip()
            if command_result.error is None:
                command_result.error = error_details(error)
            return
        info = json.loads((path / records.RUN_FILE).read_text(encoding="utf-8"))
        command_result.run_dir = str(path)
        command_result.files = {str(path / records.RUN_FILE): "the run record (run.json)"}
        command_result.files.update(
            {str(path / name): what for name, what in info["files"].items()}
        )


# -- helpers ------------------------------------------------------------------------------


def _stats(samples: list[Sample], name: str) -> dict[str, Any]:
    values = [sample.values[name] for sample in samples if name in sample.values]
    good = [value for value in values if _finite(value)]
    return {
        "count": len(values),
        "bad": len(values) - len(good),
        "median": statistics.median(good) if good else None,
        "lowest": min(good) if good else None,
        "highest": max(good) if good else None,
    }


def _finite(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _error(error: BaseException) -> str:
    return f"{type(error).__name__}: {error}"


def _and(names: list[str], word: str = "and") -> str:
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + f" {word} " + names[-1]


def _or(names: list[str]) -> str:
    return _and(names, "or")


def _deg(value_rad: float) -> str:
    return format_quantity(value_rad, Dimension.ANGLE, "deg", digits=3)


def _deg_signed(value_rad: float | None) -> str:
    if value_rad is None:
        return "an unknown amount"
    text = _deg(value_rad)
    return text if text.startswith("-") else "+" + text


def _v(value_v: float) -> str:
    return format_quantity(value_v, Dimension.VOLTAGE, digits=3)


def _degc(value_degc: float) -> str:
    return format_quantity(value_degc, Dimension.TEMPERATURE, digits=3)


def _degc_signed(value_degc: float) -> str:
    text = _degc(value_degc)
    return text if text.startswith("-") else "+" + text


def _hz(value_hz: float) -> str:
    return format_quantity(value_hz, Dimension.FREQUENCY, digits=3)


def _s(value_s: float) -> str:
    return format_quantity(value_s, Dimension.TIME, "s", digits=3)
