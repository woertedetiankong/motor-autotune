"""The adapters the conformance suite runs against.

To run the suite on a new adapter, add a ``ConformanceTarget`` to ``TARGETS``.
A target for real hardware must be opt-in (behind a marker or an option that is
off by default), because the tests marked ``motion`` move the actuator. For a
read-only run, deselect them: ``pytest tests/conformance -m "not motion"``.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterator
from dataclasses import dataclass

import pytest

from servobench.adapters.sim import SimServo
from servobench.contract import Adapter


@dataclass(frozen=True)
class ConformanceTarget:
    name: str
    make: Callable[[], Adapter]  # a fresh adapter, at rest, with no fault
    move_rad: float  # size of the small test move; small enough to be harmless
    settle_s: float  # long enough for a small move to finish


TARGETS = (ConformanceTarget("sim", SimServo, move_rad=math.radians(5.0), settle_s=0.5),)


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
