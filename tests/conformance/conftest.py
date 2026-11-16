"""The adapters the conformance suite runs against.

To run the suite on a new adapter, add a ``ConformanceTarget`` to ``TARGETS``.
A target for real hardware must be opt-in (behind a marker or an option that is
off by default), because the tests marked ``motion`` move the actuator. For a
read-only run, deselect them: ``pytest tests/conformance -m "not motion"``.

``make_silent`` gives an adapter whose device does not answer, to hold the
adapter to its declared call time when replies never come: on the simulated
servo, the dropped-replies fault; on real hardware, for example, an id that no
device on the bus has, or the device unpowered with the link still connected.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterator
from dataclasses import dataclass

import pytest

from servobench.adapters.sim import DROPPED_REPLIES, SimConfig, SimServo
from servobench.contract import Adapter


@dataclass(frozen=True)
class ConformanceTarget:
    name: str
    make: Callable[[], Adapter]  # a fresh adapter, at rest, with no fault
    move_rad: float  # size of the small test move; small enough to be harmless
    settle_s: float  # long enough for a small move to finish
    # An adapter whose device does not answer (see above); None if there is no
    # way to arrange one, and the tests that need it are skipped.
    make_silent: Callable[[], Adapter] | None = None


def _silent_sim() -> Adapter:
    return SimServo(SimConfig(faults=frozenset({DROPPED_REPLIES}), dropped_reply_probability=0.9))


TARGETS = (
    ConformanceTarget(
        "sim", SimServo, move_rad=math.radians(5.0), settle_s=0.5, make_silent=_silent_sim
    ),
)


@pytest.fixture(params=TARGETS, ids=lambda target: target.name)
def target(request) -> ConformanceTarget:
    return request.param


@pytest.fixture
def adapter(target: ConformanceTarget) -> Iterator[Adapter]:
    device = target.make()
    try:
        yield device
    finally:
        try:
            device.enter_safe_state()
        finally:
            device.close()


@pytest.fixture
def silent(target: ConformanceTarget) -> Iterator[Adapter]:
    if target.make_silent is None:
        pytest.skip(f"{target.name}: no way to arrange a device that does not answer")
    device = target.make_silent()
    try:
        yield device
    finally:
        device.close()
