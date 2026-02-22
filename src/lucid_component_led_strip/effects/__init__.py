"""
Effects package — orchestration and individual effect implementations.

``EffectOrchestrator`` manages the background thread lifecycle. Each
sub-module (glow, wave, etc.) exposes a single ``run(hardware, cancel_event,
**params)`` function that loops until cancel_event is set.

Usage::

    from lucid_component_led_strip.effects import EffectOrchestrator
    from lucid_component_led_strip.effects import glow, rainbow

    orch = EffectOrchestrator()
    orch.start("glow", glow.run, hardware, r=255, g=0, b=0, wait_ms=10)
    orch.stop()
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Callable

from . import (
    glow,
    wave,
    color_wipe,
    color_fade,
    sparkle,
    rainbow,
    rainbow_cycle,
    theater_chase,
    running,
)

__all__ = [
    "EffectOrchestrator",
    "glow",
    "wave",
    "color_wipe",
    "color_fade",
    "sparkle",
    "rainbow",
    "rainbow_cycle",
    "theater_chase",
    "running",
]

logger = logging.getLogger(__name__)


class EffectOrchestrator:
    """
    Manages a single background effect thread with clean cancellation.

    Only one effect runs at a time. Starting a new effect automatically
    stops the previous one via a shared ``threading.Event``.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cancel_event = threading.Event()
        self._effect_thread: threading.Thread | None = None
        self._current_effect: str | None = None

    @property
    def current_effect(self) -> str | None:
        with self._lock:
            return self._current_effect

    def start(
        self,
        effect_name: str,
        effect_fn: Callable[..., None],
        hardware: Any,
        **params: Any,
    ) -> None:
        """Stop any running effect and start a new one in a daemon thread."""
        self.stop()
        with self._lock:
            self._cancel_event.clear()
            self._current_effect = effect_name
            self._effect_thread = threading.Thread(
                target=self._run,
                args=(effect_name, effect_fn, hardware),
                kwargs=params,
                daemon=True,
            )
            self._effect_thread.start()
        logger.info("Started effect '%s'", effect_name)

    def stop(self, timeout: float = 2.0) -> None:
        """Signal the current effect to stop and wait for the thread to finish."""
        with self._lock:
            thread = self._effect_thread
            if thread is None:
                return
            self._cancel_event.set()

        logger.debug("Stopping effect '%s' (timeout=%.1fs)", self._current_effect, timeout)
        thread.join(timeout=timeout)

        with self._lock:
            if self._effect_thread is thread:
                self._effect_thread = None
                self._current_effect = None
            self._cancel_event.clear()

        if thread.is_alive():
            logger.warning("Effect thread did not stop within %.1fs timeout", timeout)
        else:
            logger.debug("Effect thread stopped cleanly")

    def _run(
        self,
        effect_name: str,
        effect_fn: Callable[..., None],
        hardware: Any,
        **params: Any,
    ) -> None:
        try:
            effect_fn(hardware, self._cancel_event, **params)
        except Exception:
            logger.exception("Unhandled exception in effect '%s'", effect_name)
        finally:
            with self._lock:
                if self._current_effect == effect_name:
                    self._effect_thread = None
                    self._current_effect = None
