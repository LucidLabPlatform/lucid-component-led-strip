"""
LED strip hardware controller for WS281x strips on Raspberry Pi.

Manages two strips as a single logical ring. Strip 2 is reversed so that
pixel indices wrap continuously around the ring from strip 1 end to strip 2 end.
Exposes low-level pixel helpers used by all effect modules.

Requires rpi-ws281x (required dependency; installs when the wheel is installed on the Pi).
On macOS, use make setup-venv (installs with --no-deps) for tests and wheel build only.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _get_ws281x():
    """Import rpi_ws281x only when hardware is used (Raspberry Pi). Fails on macOS if [pi] not installed."""
    try:
        from rpi_ws281x import Adafruit_NeoPixel, Color
        return Adafruit_NeoPixel, Color
    except ImportError as e:
        raise ImportError(
            "rpi-ws281x is required for LED hardware. On Raspberry Pi run: pip install .[pi]"
        ) from e


class LEDStripHardware:
    """
    Controls two WS281x LED strips as a single logical ring.

    Default wiring matches the OptiTrack truss installation:
    - Strip 1: 896 LEDs on GPIO18 (PWM channel 0)
    - Strip 2: 894 LEDs on GPIO13 (PWM channel 1)
    - Total: 1790 LEDs, DMA channel 10, 800 kHz
    """

    def __init__(
        self,
        strip1_count: int = 896,
        strip2_count: int = 894,
        strip1_pin: int = 18,
        strip2_pin: int = 13,
        freq: int = 800_000,
        dma: int = 10,
        brightness: int = 125,
    ) -> None:
        self.STRIP1_COUNT = strip1_count
        self.STRIP2_COUNT = strip2_count
        self.LED_COUNT = strip1_count + strip2_count
        self.STRIP1_PIN = strip1_pin
        self.STRIP2_PIN = strip2_pin
        self.LED_FREQ_HZ = freq
        self.LED_DMA = dma
        self.LED_BRIGHTNESS = brightness
        self.LED_INVERT = False

        Adafruit_NeoPixel, self._Color = _get_ws281x()

        self._strip1 = Adafruit_NeoPixel(
            self.STRIP1_COUNT,
            self.STRIP1_PIN,
            self.LED_FREQ_HZ,
            self.LED_DMA,
            self.LED_INVERT,
            self.LED_BRIGHTNESS,
            0,
        )
        self._strip2 = Adafruit_NeoPixel(
            self.STRIP2_COUNT,
            self.STRIP2_PIN,
            self.LED_FREQ_HZ,
            self.LED_DMA,
            self.LED_INVERT,
            self.LED_BRIGHTNESS,
            1,
        )

        self._strip1.begin()
        self._strip2.begin()
        # Software buffer of last-written [r,g,b] per pixel (for get_pixels readback)
        self._pixel_buffer: list[list[int]] = [[0, 0, 0] for _ in range(self.LED_COUNT)]
        logger.info(
            "LED strips initialized: %d LEDs on GPIO%d, %d LEDs on GPIO%d",
            self.STRIP1_COUNT, self.STRIP1_PIN,
            self.STRIP2_COUNT, self.STRIP2_PIN,
        )

    # ------------------------------------------------------------------
    # Low-level pixel helpers
    # ------------------------------------------------------------------

    def _color_to_rgb(self, color: Any) -> tuple[int, int, int]:
        """Convert Color or 32-bit int to (r, g, b)."""
        if hasattr(color, "__iter__") and not isinstance(color, (int, float)):
            parts = list(color)
            return (int(parts[0]) & 0xFF, int(parts[1]) & 0xFF, int(parts[2]) & 0xFF)
        c = int(color)
        return ((c >> 16) & 0xFF, (c >> 8) & 0xFF, c & 0xFF)

    def set_pixel_color(self, pixel_index: int, color: Any) -> None:
        """Set a single pixel by logical ring index."""
        if 0 <= pixel_index < self.LED_COUNT:
            self._pixel_buffer[pixel_index] = list(self._color_to_rgb(color))
        if pixel_index < self.STRIP1_COUNT:
            self._strip1.setPixelColor(pixel_index, color)
        else:
            # Strip 2 runs in reverse so the ring is continuous.
            reversed_index = self.STRIP2_COUNT - (pixel_index - self.STRIP1_COUNT) - 1
            self._strip2.setPixelColor(reversed_index, color)

    def show(self) -> None:
        """Push pixel buffer to both strips."""
        self._strip1.show()
        self._strip2.show()

    def set_brightness(self, brightness: int) -> None:
        """Set brightness on both strips (0-255)."""
        self.LED_BRIGHTNESS = brightness
        self._strip1.setBrightness(brightness)
        self._strip2.setBrightness(brightness)

    def set_color_all(self, color: Any) -> None:
        """Set every LED to the same color and push immediately."""
        for i in range(self.LED_COUNT):
            self.set_pixel_color(i, color)
        self.show()

    def set_white_all(self) -> None:
        self.set_color_all(self._Color(255, 255, 255))

    def clear_all(self) -> None:
        """Turn off all LEDs."""
        self.set_color_all(self._Color(0, 0, 0))

    def get_pixel_buffer(self) -> list[list[int]]:
        """Return current pixel buffer as list of [r,g,b] per pixel."""
        return [list(p) for p in self._pixel_buffer]

    def set_color_range_percent(
        self, color: Any, start_percent: float, end_percent: float
    ) -> None:
        """Set color for a range defined by start/end as fractions of 0.0-1.0."""
        start_index = int(self.LED_COUNT * start_percent)
        end_index = int(self.LED_COUNT * end_percent)
        index_range = (
            end_index - start_index
            if end_index >= start_index
            else self.LED_COUNT - end_index + start_index
        )
        for i in range(index_range):
            self.set_pixel_color((start_index + i) % self.LED_COUNT, color)
        self.show()

    def set_color_range_exact(
        self, color: Any, start_index: int, end_index: int
    ) -> None:
        """Set color for a range defined by exact LED indices."""
        index_range = (
            end_index - start_index
            if end_index >= start_index
            else self.LED_COUNT - end_index + start_index
        )
        for i in range(index_range):
            self.set_pixel_color((start_index + i) % self.LED_COUNT, color)
        self.show()

    def wheel(self, pos: int) -> Any:
        """Generate a rainbow color from a position 0-255."""
        pos = pos & 255
        if pos < 85:
            return self._Color(pos * 3, 255 - pos * 3, 0)
        if pos < 170:
            pos -= 85
            return self._Color(255 - pos * 3, 0, pos * 3)
        pos -= 170
        return self._Color(0, pos * 3, 255 - pos * 3)
