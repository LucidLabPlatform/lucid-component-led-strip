"""
Color wipe effect — sets pixels one at a time from start to end.

Does not loop; completes a single pass then exits.
"""
from __future__ import annotations

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
    wait_ms: int = 50,
) -> None:
    """
    Args:
        hardware: LED strip hardware instance.
        cancel_event: Set to stop the effect early.
        r, g, b: Wipe color components (0-255).
        wait_ms: Delay per pixel in milliseconds.
    """
    color = Color(r, g, b)
    for i in range(hardware.LED_COUNT):
        if cancel_event.is_set():
            return
        hardware.set_pixel_color(i, color)
        hardware.show()
        time.sleep(wait_ms / 1000.0)
