"""Adapters: the only code that knows a particular actuator. See ``servobench.contract``.

``ADAPTERS`` maps the name written in the bench file's ``[target] adapter`` to
the class that drives it, as ``"module:Class"``. Each class provides
``from_bench(bench)``, which returns the adapter for the bench file's target
(port, id, load). A class is imported only when its name is used, so an adapter
for hardware costs nothing to the others.

To add an adapter, add one line here.
"""

from __future__ import annotations

import importlib

from servobench.contract import Adapter
from servobench.limits import Bench

ADAPTERS: dict[str, str] = {
    "sim": "servobench.adapters.sim:SimServo",
}


class UnknownAdapter(LookupError):
    """The bench file names an adapter that does not exist."""

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(
            f"[target] adapter {name!r} is not a known adapter; known adapters:"
            f" {', '.join(sorted(ADAPTERS))}"
        )


def adapter_class(name: str) -> type[Adapter]:
    """The class registered under ``name``. Raises ``UnknownAdapter``."""
    location = ADAPTERS.get(name)
    if location is None:
        raise UnknownAdapter(name)
    module_name, _, class_name = location.partition(":")
    return getattr(importlib.import_module(module_name), class_name)


def open_adapter(bench: Bench) -> Adapter:
    """The adapter for the bench file's target, created from the bench file.

    Raises ``UnknownAdapter``, or an ``AdapterError`` if the actuator cannot be
    reached (for a real adapter, a port that cannot be opened). Creating an
    adapter changes nothing on the actuator.
    """
    return adapter_class(bench.target.adapter).from_bench(bench)
