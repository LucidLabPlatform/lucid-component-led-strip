"""
Wave effect — moves cosine waves of the given color across all LEDs.

Runs continuously until cancel_event is set.
"""
from __future__ import annotations

import time
import threading

import numpy as np
from rpi_ws281x import Color

from lucid_component_led_strip.hardware import LEDStripHardware


def run(
    hardware: LEDStripHardware,
    cancel_event: threading.Event,
    r: int = 255,
    g: int = 255,
    b: int = 255,
    cycles: int = 1,
    speed: float = 0.1,
    wait_ms: int = 10,
) -> None:
    """
    Args:
        hardware: LED strip hardware instance.
        cancel_event: Set to stop the effect.
        r, g, b: Wave color components (0-255).
        cycles: Number of wave cycles visible across the strip.
        speed: Phase advance per frame (higher = faster).
        wait_ms: Delay between frames in milliseconds.
    """
    frame = 0
    color_base = np.array([r, g, b], dtype=np.uint8)

    while not cancel_event.is_set():
        cos_lookup = (
            np.cos(
                np.linspace(
                    np.pi - (frame * speed),
                    (np.pi * (cycles * 3)) - (frame * speed),
                    hardware.LED_COUNT,
                )
            )
            + 1
        ) * 0.5

        color_table = np.multiply(
            np.tile(color_base, [hardware.LED_COUNT, 1]),
            cos_lookup[:, np.newaxis],
        ).astype(int)

        for j in range(hardware.LED_COUNT):
            hardware.set_pixel_color(
                j, Color(int(color_table[j][0]), int(color_table[j][1]), int(color_table[j][2]))
            )

        hardware.show()
        frame += 1
        time.sleep(wait_ms / 1000.0)
