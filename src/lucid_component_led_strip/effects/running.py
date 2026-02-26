"""
Running effect — a colored dot chases around the ring continuously.

Runs continuously until cancel_event is set.
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
    wait_ms: int = 10,
    width: int = 1,
) -> None:
    """
    Args:
        hardware: LED strip hardware instance.
        cancel_event: Set to stop the effect.
        r, g, b: Dot color components (0-255).
        wait_ms: Delay between steps in milliseconds.
        width: Number of pixels in the running dot.
    """
    hardware.clear_all()
    index = 0
    dot_color = Color(r, g, b)
    while not cancel_event.is_set():
        hardware.set_pixel_color((index - width) % hardware.LED_COUNT, Color(0, 0, 0))
        hardware.set_pixel_color(index, dot_color)
        hardware.show()
        index = (index + 1) % hardware.LED_COUNT
        time.sleep(wait_ms / 1000.0)
