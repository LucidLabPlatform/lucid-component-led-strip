"""
Color fade effect — linearly interpolates all LEDs from one color to another.

Does not loop; completes a single fade then exits.
"""
from __future__ import annotations

import time
import threading

from rpi_ws281x import Color

from lucid_component_led_strip.hardware import LEDStripHardware


def run(
    hardware: LEDStripHardware,
    cancel_event: threading.Event,
    r_from: int = 0,
    g_from: int = 0,
    b_from: int = 0,
    r_to: int = 255,
    g_to: int = 255,
    b_to: int = 255,
    wait_ms: int = 20,
    steps: int = 100,
) -> None:
    """
    Args:
        hardware: LED strip hardware instance.
        cancel_event: Set to stop the effect early.
        r_from, g_from, b_from: Starting color (0-255 each).
        r_to, g_to, b_to: Ending color (0-255 each).
        wait_ms: Delay between steps in milliseconds.
        steps: Total number of interpolation steps.
    """
    step_r = (r_to - r_from) / steps
    step_g = (g_to - g_from) / steps
    step_b = (b_to - b_from) / steps

    r, g, b = float(r_from), float(g_from), float(b_from)

    for _ in range(steps):
        if cancel_event.is_set():
            return
        c = Color(int(r), int(g), int(b))
        for i in range(hardware.LED_COUNT):
            hardware.set_pixel_color(i, c)
        hardware.show()
        time.sleep(wait_ms / 1000.0)
        r += step_r
        g += step_g
        b += step_b
