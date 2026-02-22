"""
Rainbow effect — fades a shifting rainbow across all pixels simultaneously.

Each pixel gets the same hue offset, so the whole strip changes color together.
Runs continuously until cancel_event is set.
"""
from __future__ import annotations

import time
import threading

from lucid_component_led_strip.hardware import LEDStripHardware


def run(
    hardware: LEDStripHardware,
    cancel_event: threading.Event,
    wait_ms: int = 50,
) -> None:
    """
    Args:
        hardware: LED strip hardware instance.
        cancel_event: Set to stop the effect.
        wait_ms: Delay between frames in milliseconds.
    """
    j = 0
    while not cancel_event.is_set():
        for i in range(hardware.LED_COUNT):
            hardware.set_pixel_color(i, hardware.wheel((i + j) & 255))
        hardware.show()
        j = (j + 1) % 256
        time.sleep(wait_ms / 1000.0)
