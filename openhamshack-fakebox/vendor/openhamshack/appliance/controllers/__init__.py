"""Controller drivers, one module per family of hardware.

Adding a model: write a Driver subclass (see base.py; ajazz.py is the
worked example, keypad.py one with no screens) and add it to DRIVERS. The
console draws it from its `controls`, the mapping is checked against them,
and its `default_map` is what a new box does with it - nothing else in
OpenHamShack names a model.
"""

from __future__ import annotations

from . import ajazz, keypad
from .base import Driver

# Order matters only for finding: a dedicated model is looked for before the
# generic keypad, so its keyboard face is never taken for one.
DRIVERS: list[type[Driver]] = [ajazz.AKP03E, keypad.Keypad]

keypad.SKIP_USB.update(ids for d in DRIVERS if not d.open_layout for ids in d.usb)


def by_model(model: str) -> type[Driver] | None:
    return next((d for d in DRIVERS if d.model == model), None)
