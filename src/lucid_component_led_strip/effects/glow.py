"""
Glow effect — pulses all LEDs with a smooth cosine brightness envelope.

Runs continuously until cancel_event is set.
"""
from __future__ import annotations

import math
import time
import threading

from rpi_ws281x import Color

from lucid_component_led_strip.hardware import LEDStripHardware


def run(
    hardware: LEDStripHardware,
    cancel_event: threading.Event,
    r: int = 255,
    g: int = 255,
    b: int = 255,
    wait_ms: int = 10,
) -> None:
    """
    Args:
        hardware: LED strip hardware instance.
        cancel_event: Set to stop the effect.
        r, g, b: Base color components (0-255).
        wait_ms: Delay between frames in milliseconds.
    """
    phase = 0.0
    while not cancel_event.is_set():
        brightness_scale = (1.0 - math.cos(phase)) * 0.5
        c = Color(
            int(r * brightness_scale),
            int(g * brightness_scale),
            int(b * brightness_scale),
        )
        for i in range(hardware.LED_COUNT):
            hardware.set_pixel_color(i, c)
        hardware.show()
        phase += 0.1
        time.sleep(wait_ms / 1000.0)
