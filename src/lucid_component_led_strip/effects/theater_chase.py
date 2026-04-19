"""
Theater chase effect — every third pixel lights in a chasing pattern.

Single-color mode: all dots share one color.
Rainbow mode (r=g=b=-1 sentinel): dots cycle through rainbow hues.

Runs continuously until cancel_event is set.
"""
from __future__ import annotations

import time
import threading

from rpi_ws281x import Color

from lucid_component_led_strip.hardware import LEDStripHardware

_RAINBOW_SENTINEL = -1


def run(
    hardware: LEDStripHardware,
    cancel_event: threading.Event,
    r: int = _RAINBOW_SENTINEL,
    g: int = _RAINBOW_SENTINEL,
    b: int = _RAINBOW_SENTINEL,
    wait_ms: int = 50,
    spacing: int = 3,
    width: int = 1,
) -> None:
    """
    Args:
        hardware: LED strip hardware instance.
        cancel_event: Set to stop the effect.
        r, g, b: Chase color (0-255 each). Pass -1 for all three for rainbow mode.
        wait_ms: Delay between positions in milliseconds.
        spacing: Pixels between the start of each dot group (default 3).
        width: Number of lit pixels per dot (default 1). Should be < spacing.
    """
    rainbow_mode = (r == _RAINBOW_SENTINEL and g == _RAINBOW_SENTINEL and b == _RAINBOW_SENTINEL)
    j = 0

    while not cancel_event.is_set():
        for q in range(spacing):
            if cancel_event.is_set():
                return
            lit = []
            for i in range(0, hardware.LED_COUNT, spacing):
                for w in range(width):
                    idx = (i + q + w) % hardware.LED_COUNT
                    pixel_color = hardware.wheel((i + j) % 255) if rainbow_mode else Color(r, g, b)
                    hardware.set_pixel_color(idx, pixel_color)
                    lit.append(idx)
            hardware.show()
            time.sleep(wait_ms / 1000.0)
            for idx in lit:
                hardware.set_pixel_color(idx, Color(0, 0, 0))
        j = (j + 1) % 256
