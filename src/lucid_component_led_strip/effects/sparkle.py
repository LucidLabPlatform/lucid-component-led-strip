"""
Sparkle effect — randomly lights individual pixels.

Single-color mode: all sparkles share one color.
Multicolor mode (r=g=b=-1 sentinel): each sparkle gets a random color.

Completes one full pass over LED_COUNT pixels per call (does not loop),
matching the original led_truss behavior.
"""
from __future__ import annotations

import random
import time
import threading

from rpi_ws281x import Color

from lucid_component_led_strip.hardware import LEDStripHardware

_MULTICOLOR_SENTINEL = -1


def run(
    hardware: LEDStripHardware,
    cancel_event: threading.Event,
    r: int = _MULTICOLOR_SENTINEL,
    g: int = _MULTICOLOR_SENTINEL,
    b: int = _MULTICOLOR_SENTINEL,
    wait_ms: int = 50,
    cumulative: bool = False,
) -> None:
    """
    Args:
        hardware: LED strip hardware instance.
        cancel_event: Set to stop the effect early.
        r, g, b: Sparkle color (0-255 each). Pass -1 for all three to use
                 random multicolor sparkles.
        wait_ms: Delay between sparkles in milliseconds.
        cumulative: If True, pixels accumulate; if False, each frame clears first.
    """
    multicolor = (r == _MULTICOLOR_SENTINEL and g == _MULTICOLOR_SENTINEL and b == _MULTICOLOR_SENTINEL)
    hardware.clear_all()

    for _ in range(hardware.LED_COUNT):
        if cancel_event.is_set():
            return
        if multicolor:
            c = Color(random.randrange(256), random.randrange(256), random.randrange(256))
        else:
            c = Color(r, g, b)
        hardware.set_pixel_color(random.randrange(hardware.LED_COUNT), c)
        hardware.show()
        time.sleep(wait_ms / 1000.0)
        if not cumulative:
            hardware.clear_all()

    time.sleep(wait_ms / 1000.0)
