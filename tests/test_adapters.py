"""The adapter registry: the bench file's adapter name to the class that drives it."""

import pytest

from servobench import adapters
from servobench.adapters import UnknownAdapter, adapter_class, open_adapter
from servobench.adapters.sim import SimServo
from servobench.contract import Adapter
from servobench.limits import TEMPLATE, parse_bench


def test_every_registered_adapter_is_an_adapter_built_from_the_bench_file():
    for name in adapters.ADAPTERS:
        cls = adapter_class(name)
        assert issubclass(cls, Adapter) and callable(cls.from_bench), name


def test_the_bench_file_opens_the_simulated_servo_with_its_load():
    servo = open_adapter(parse_bench(TEMPLATE.encode()))
    assert isinstance(servo, SimServo)
    assert servo.describe().identity.adapter == "sim"
    assert servo._load.mass_kg == pytest.approx(0.05)


def test_an_unknown_adapter_names_the_known_ones():
    expected = "'hd1910' is not a known adapter; known adapters: sim"
    with pytest.raises(UnknownAdapter, match=expected):
        adapter_class("hd1910")
