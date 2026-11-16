import math

import pytest

from servobench.clock import MonotonicClock, VirtualClock


def test_virtual_clock_moves_only_when_waited_on():
    clock = VirtualClock()
    assert clock.now_s() == 0.0
    assert clock.now_s() == 0.0
    clock.wait_s(0.25)
    assert clock.now_s() == pytest.approx(0.25)
    clock.wait_until_s(10.0)
    assert clock.now_s() == 10.0


def test_virtual_clock_never_goes_backwards():
    clock = VirtualClock(start_s=5.0)
    clock.wait_until_s(1.0)
    assert clock.now_s() == 5.0
    clock.wait_s(0.0)
    assert clock.now_s() == 5.0


@pytest.mark.parametrize("bad", [-1.0, math.nan, math.inf])
def test_wait_rejects_a_bad_duration(bad):
    with pytest.raises(ValueError):
        VirtualClock().wait_s(bad)


@pytest.mark.parametrize("bad", [math.nan, math.inf])
def test_wait_until_rejects_a_bad_time(bad):
    with pytest.raises(ValueError):
        VirtualClock().wait_until_s(bad)
    with pytest.raises(ValueError):
        MonotonicClock().wait_until_s(bad)


def test_monotonic_clock_uses_the_monotonic_clock_and_sleep(monkeypatch):
    # Stand-ins for the operating system, so this test neither reads the real
    # clock nor sleeps.
    now = [100.0]
    slept = []

    def fake_sleep(duration_s):
        slept.append(duration_s)
        now[0] += duration_s

    monkeypatch.setattr("servobench.clock.time.monotonic", lambda: now[0])
    monkeypatch.setattr("servobench.clock.time.sleep", fake_sleep)
    clock = MonotonicClock()
    assert clock.now_s() == 100.0
    clock.wait_s(0.5)
    assert slept == [pytest.approx(0.5)]
    clock.wait_until_s(50.0)  # in the past: no sleep
    assert len(slept) == 1
