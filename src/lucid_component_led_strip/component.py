"""
LEDStripComponent — LUCID component for WS281x LED strip control.

Talks to the root-owned LED helper daemon over Unix socket (see protocol.py).
Publishes retained: metadata, status, state, cfg.
Commands: cmd/reset, cmd/ping, cmd/clear (→ evt/clear/result), cmd/cfg/set.
Effect commands: cmd/effect/<name> → evt/effect/<name>/result.
Telemetry: pixel_rgb (array of current [r,g,b] per pixel).

Hardware defaults match the current OptiTrack truss installation of floor T5:
  Strip 1: 896 LEDs on GPIO18   Strip 2: 894 LEDs on GPIO13
  Total: 1790 LEDs, 800 kHz, DMA 10, initial brightness 125.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any

from lucid_component_base import Component, ComponentContext, ComponentStatus

from . import client as led_client


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clamp(value: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, value))


_IPC_ERROR_MARKERS = ("[Errno ", "connection closed")


def _is_ipc_failure(error: str | None) -> bool:
    if not error:
        return False
    return any(m in error for m in _IPC_ERROR_MARKERS)


class LEDStripComponent(Component):
    """
    LUCID component for WS281x LED strip control.

    State topics reflect live hardware state. Custom effect commands follow
    the pattern cmd/effect/<name> with JSON payloads matching effects_map
    parameters. All results are published to evt/effect/<name>/result.
    """

    # Command/effect names registered as LUCID capabilities.
    # Top-level commands (no effect/ prefix): clear, set-color, set-range-*.
    # Effects (streaming, looping): effect/<name>.
    CAPABILITIES = [
        "clear",
        "set-color",
        "set-range-percent",
        "set-range-exact",
        "effect/glow",
        "effect/wave",
        "effect/color-wipe",
        "effect/color-fade",
        "effect/sparkle",
        "effect/rainbow",
        "effect/rainbow-cycle",
        "effect/theater-chase",
        "effect/running",
    ]

    def __init__(self, context: ComponentContext) -> None:
        super().__init__(context)
        self._log = context.logger()

        cfg = context.config

        # Hardware configuration — defaults match the truss installation.
        self._strip1_count: int = int(cfg.get("strip1_count", 896))
        self._strip2_count: int = int(cfg.get("strip2_count", 894))
        self._strip1_pin: int = int(cfg.get("strip1_pin", 18))
        self._strip2_pin: int = int(cfg.get("strip2_pin", 13))
        self._brightness: int = _clamp(int(cfg.get("brightness", 125)), 0, 255)

        self._hardware_initialized = False
        self._current_effect: str | None = None
        self._pixel_telemetry_stop = threading.Event()
        self._pixel_telemetry_thread: threading.Thread | None = None
        self._pixel_telemetry_interval_s = 5

    # ------------------------------------------------------------------
    # LUCID contract
    # ------------------------------------------------------------------

    @property
    def component_id(self) -> str:
        return "led_strip"

    def capabilities(self) -> list[str]:
        return ["reset", "ping"] + self.CAPABILITIES

    def get_cfg_payload(self) -> dict[str, Any]:
        return {
            "brightness": self._brightness,
            "strip1_count": self._strip1_count,
            "strip2_count": self._strip2_count,
            "strip1_pin": self._strip1_pin,
            "strip2_pin": self._strip2_pin,
        }

    def get_state_payload(self) -> dict[str, Any]:
        return {
            "brightness": self._brightness,
            "current_effect": self._current_effect,
            "led_count": self._strip1_count + self._strip2_count,
            "strip1_count": self._strip1_count,
            "strip2_count": self._strip2_count,
            "strip1_pin": self._strip1_pin,
            "strip2_pin": self._strip2_pin,
            "hardware_initialized": self._hardware_initialized,
        }

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def _signal_hardware_failed(self, error: str) -> None:
        """Transition to FAILED state and publish; called on any runtime IPC failure."""
        self._hardware_initialized = False
        self._state.last_error = error
        self._set_state(ComponentStatus.FAILED)
        self.publish_state()

    def _start(self) -> None:
        result = led_client.init(
            strip1_count=self._strip1_count,
            strip2_count=self._strip2_count,
            strip1_pin=self._strip1_pin,
            strip2_pin=self._strip2_pin,
            brightness=self._brightness,
        )
        if result.get("ok"):
            self._hardware_initialized = True
            self._log.info(
                "LED helper initialized: %d LEDs (strip1=%d GPIO%d, strip2=%d GPIO%d), brightness=%d",
                self._strip1_count + self._strip2_count,
                self._strip1_count, self._strip1_pin,
                self._strip2_count, self._strip2_pin,
                self._brightness,
            )
        else:
            self._hardware_initialized = False
            error_msg = result.get("error", "unknown")
            self._log.error("LED helper init failed: %s", error_msg)
            self._publish_all_retained()
            raise RuntimeError(f"LED helper init failed: {error_msg}")

        self._publish_all_retained()

    def _stop(self) -> None:
        self._stop_pixel_telemetry()
        led_client.reset()
        self._current_effect = None
        self._hardware_initialized = False
        self._log.info("Stopped component: %s", self.component_id)

    def _pixel_telemetry_loop(self) -> None:
        while not self._pixel_telemetry_stop.wait(timeout=self._pixel_telemetry_interval_s):
            if not self._hardware_initialized:
                continue
            try:
                result = led_client.get_pixels()
                if result.get("ok") and "pixels" in result:
                    self.publish_telemetry("pixel_rgb", result["pixels"])
                    self._log.debug("Published pixel_rgb telemetry (%d pixels)", len(result["pixels"]))
                elif not result.get("ok") and _is_ipc_failure(result.get("error")):
                    self._log.error("pixel_rgb telemetry IPC failure: %s", result.get("error"))
                    self._signal_hardware_failed(result.get("error", "IPC failure"))
            except Exception as e:
                self._log.debug("pixel_rgb telemetry failed: %s", e)

    def _start_pixel_telemetry(self) -> None:
        if self._pixel_telemetry_thread is not None:
            return
        self._pixel_telemetry_stop.clear()
        self._pixel_telemetry_thread = threading.Thread(
            target=self._pixel_telemetry_loop,
            daemon=True,
            name="led-strip-pixel-telemetry",
        )
        self._pixel_telemetry_thread.start()
        self._log.debug("Started pixel_rgb telemetry thread (interval=%ss)", self._pixel_telemetry_interval_s)

    def _stop_pixel_telemetry(self) -> None:
        self._pixel_telemetry_stop.set()
        if self._pixel_telemetry_thread is not None:
            self._pixel_telemetry_thread.join(timeout=2.0)
            self._pixel_telemetry_thread = None

    def _publish_all_retained(self) -> None:
        self.publish_metadata()
        self.publish_status()
        self.publish_state()
        self.set_telemetry_config({
            "pixel_rgb": {"enabled": True, "interval_s": self._pixel_telemetry_interval_s, "change_threshold_percent": 0.0},
        })
        self.publish_cfg()
        self._start_pixel_telemetry()

    def _flash_reset(self) -> None:
        """Brief white flash then clear — visual confirmation of reset. Runs in a background thread."""
        led_client.set_color({"r": 255, "g": 255, "b": 255})
        time.sleep(0.2)
        led_client.clear()

    # ------------------------------------------------------------------
    # Standard commands
    # ------------------------------------------------------------------

    def on_cmd_reset(self, payload_str: str) -> None:
        try:
            payload = json.loads(payload_str) if payload_str else {}
            request_id = payload.get("request_id", "")
        except json.JSONDecodeError:
            request_id = ""

        self._log.info("cmd/reset request_id=%s", request_id)
        result = led_client.reset()
        if not result.get("ok") and _is_ipc_failure(result.get("error")):
            self._signal_hardware_failed(result.get("error", "IPC failure"))
        self._current_effect = None
        self.publish_state()
        self.publish_result("reset", request_id, ok=result.get("ok", True), error=result.get("error"))
        self._log.debug("cmd/reset result ok=%s", result.get("ok", True))
        if result.get("ok") and self._hardware_initialized:
            threading.Thread(target=self._flash_reset, daemon=True).start()

    def on_cmd_ping(self, payload_str: str) -> None:
        try:
            payload = json.loads(payload_str) if payload_str else {}
            request_id = payload.get("request_id", "")
        except json.JSONDecodeError:
            request_id = ""
        self._log.info("cmd/ping request_id=%s hardware_initialized=%s", request_id, self._hardware_initialized)
        self.publish_result("ping", request_id, ok=True, error=None)

    def on_cmd_cfg_set(self, payload_str: str) -> None:
        try:
            payload = json.loads(payload_str) if payload_str else {}
            request_id = payload.get("request_id", "")
            set_dict = payload.get("set") or {}
        except json.JSONDecodeError:
            self.publish_cfg_set_result(request_id="", ok=False, applied=None, error="invalid JSON", ts=_utc_iso())
            return

        self._log.info("cmd/cfg/set request_id=%s set_keys=%s", request_id, list(set_dict.keys()) if isinstance(set_dict, dict) else None)
        if not isinstance(set_dict, dict):
            self.publish_cfg_set_result(
                request_id=request_id,
                ok=False,
                applied=None,
                error="payload 'set' must be an object",
                ts=_utc_iso(),
            )
            return

        applied: dict[str, Any] = {}
        restart_required = False

        if "log_level" in set_dict:
            self.apply_log_level(str(set_dict["log_level"]))
            applied["log_level"] = self._log_level

        # Runtime-applicable config
        if "brightness" in set_dict:
            self._brightness = _clamp(int(set_dict["brightness"]), 0, 255)
            applied["brightness"] = self._brightness
            if self._hardware_initialized:
                r = led_client.set_brightness(self._brightness)
                if not r.get("ok"):
                    if _is_ipc_failure(r.get("error")):
                        self._signal_hardware_failed(r.get("error", "IPC failure"))
                    else:
                        applied["brightness_error"] = r.get("error")

        # Hardware config — requires restart to take effect
        for key in ("strip1_count", "strip2_count", "strip1_pin", "strip2_pin"):
            if key in set_dict:
                setattr(self, f"_{key}", int(set_dict[key]))
                applied[key] = getattr(self, f"_{key}")
                restart_required = True

        self.publish_state()
        self.publish_cfg()
        self.publish_cfg_set_result(
            request_id=request_id,
            ok=True,
            applied=applied if applied else None,
            error="restart required for hardware config changes" if restart_required else None,
            ts=_utc_iso(),
        )

    # ------------------------------------------------------------------
    # Effect command helpers
    # ------------------------------------------------------------------

    def _require_hardware(self) -> bool:
        return self._hardware_initialized

    def _parse_effect_payload(self, payload_str: str) -> tuple[str, dict[str, Any]]:
        """Return (request_id, params_dict). Raises ValueError on bad JSON."""
        payload = json.loads(payload_str) if payload_str else {}
        return payload.get("request_id", ""), {k: v for k, v in payload.items() if k != "request_id"}

    def _publish_effect_result(
        self,
        effect_name: str,
        request_id: str,
        ok: bool,
        error: str | None = None,
    ) -> None:
        topic = self.context.topic(f"evt/effect/{effect_name}/result")
        result = {"request_id": request_id, "ok": ok, "error": error}
        self.context.mqtt.publish(topic, json.dumps(result), qos=1, retain=False)

    # ------------------------------------------------------------------
    # Effect command handlers
    # ------------------------------------------------------------------

    def on_cmd_clear(self, payload_str: str) -> None:
        try:
            payload = json.loads(payload_str) if payload_str else {}
            request_id = payload.get("request_id", "")
        except json.JSONDecodeError:
            request_id = ""

        self._log.info("cmd/clear request_id=%s", request_id)
        if not self._require_hardware():
            self.publish_result("clear", request_id, ok=False, error="hardware not initialized")
            return
        result = led_client.clear()
        if not result.get("ok") and _is_ipc_failure(result.get("error")):
            self._signal_hardware_failed(result.get("error", "IPC failure"))
        self._current_effect = None
        self.publish_state()
        self.publish_result("clear", request_id, ok=result.get("ok", True), error=result.get("error"))
        self._log.debug("cmd/clear result ok=%s", result.get("ok", True))

    def on_cmd_set_color(self, payload_str: str) -> None:
        try:
            request_id, params = self._parse_effect_payload(payload_str)
        except (json.JSONDecodeError, ValueError):
            self.publish_result("set-color", "", ok=False, error="invalid JSON")
            return

        self._log.info("cmd/set-color request_id=%s color=%s", request_id, params.get("color"))
        if not self._require_hardware():
            self.publish_result("set-color", request_id, ok=False, error="hardware not initialized")
            return

        color_dict = params.get("color")
        color = None if not color_dict else {"r": int(color_dict["r"]), "g": int(color_dict["g"]), "b": int(color_dict["b"])}
        result = led_client.set_color(color)
        if not result.get("ok") and _is_ipc_failure(result.get("error")):
            self._signal_hardware_failed(result.get("error", "IPC failure"))
        self._current_effect = None
        self.publish_state()
        self.publish_result("set-color", request_id, ok=result.get("ok", True), error=result.get("error"))
        self._log.debug("cmd/set-color result ok=%s", result.get("ok", True))

    def on_cmd_set_range_percent(self, payload_str: str) -> None:
        try:
            request_id, params = self._parse_effect_payload(payload_str)
        except (json.JSONDecodeError, ValueError):
            self.publish_result("set-range-percent", "", ok=False, error="invalid JSON")
            return

        self._log.info("cmd/set-range-percent request_id=%s start_percent=%s end_percent=%s", request_id, params.get("start_percent"), params.get("end_percent"))
        if not self._require_hardware():
            self.publish_result("set-range-percent", request_id, ok=False, error="hardware not initialized")
            return

        color_dict = params.get("color", {"r": 255, "g": 255, "b": 255})
        result = led_client.set_range_percent(
            {"r": int(color_dict["r"]), "g": int(color_dict["g"]), "b": int(color_dict["b"])},
            float(params.get("start_percent", 0.0)),
            float(params.get("end_percent", 1.0)),
        )
        if not result.get("ok") and _is_ipc_failure(result.get("error")):
            self._signal_hardware_failed(result.get("error", "IPC failure"))
        self._current_effect = None
        self.publish_state()
        self.publish_result("set-range-percent", request_id, ok=result.get("ok", True), error=result.get("error"))

    def on_cmd_set_range_exact(self, payload_str: str) -> None:
        try:
            request_id, params = self._parse_effect_payload(payload_str)
        except (json.JSONDecodeError, ValueError):
            self.publish_result("set-range-exact", "", ok=False, error="invalid JSON")
            return

        self._log.info("cmd/set-range-exact request_id=%s start_index=%s end_index=%s", request_id, params.get("start_index"), params.get("end_index"))
        if not self._require_hardware():
            self.publish_result("set-range-exact", request_id, ok=False, error="hardware not initialized")
            return

        led_count = self._strip1_count + self._strip2_count
        color_dict = params.get("color", {"r": 255, "g": 255, "b": 255})
        result = led_client.set_range_exact(
            {"r": int(color_dict["r"]), "g": int(color_dict["g"]), "b": int(color_dict["b"])},
            int(params.get("start_index", 0)),
            int(params.get("end_index", led_count)),
        )
        if not result.get("ok") and _is_ipc_failure(result.get("error")):
            self._signal_hardware_failed(result.get("error", "IPC failure"))
        self._current_effect = None
        self.publish_state()
        self.publish_result("set-range-exact", request_id, ok=result.get("ok", True), error=result.get("error"))

    def on_cmd_effect_glow(self, payload_str: str) -> None:
        try:
            request_id, params = self._parse_effect_payload(payload_str)
        except (json.JSONDecodeError, ValueError):
            self._publish_effect_result("glow", "", ok=False, error="invalid JSON")
            return

        self._log.info("cmd/effect/glow request_id=%s wait_ms=%s", request_id, params.get("wait_ms"))
        if not self._require_hardware():
            self._publish_effect_result("glow", request_id, ok=False, error="hardware not initialized")
            return

        color_dict = params.get("color", {"r": 255, "g": 255, "b": 255})
        led_client.stop_effect()
        result = led_client.effect("glow", {"color": color_dict, "wait_ms": params.get("wait_ms", 10)})
        if not result.get("ok") and _is_ipc_failure(result.get("error")):
            self._signal_hardware_failed(result.get("error", "IPC failure"))
        if result.get("ok"):
            self._current_effect = "glow"
        self.publish_state()
        self._publish_effect_result("glow", request_id, ok=result.get("ok", True), error=result.get("error"))

    def on_cmd_effect_wave(self, payload_str: str) -> None:
        try:
            request_id, params = self._parse_effect_payload(payload_str)
        except (json.JSONDecodeError, ValueError):
            self._publish_effect_result("wave", "", ok=False, error="invalid JSON")
            return

        self._log.info("cmd/effect/wave request_id=%s cycles=%s speed=%s", request_id, params.get("cycles"), params.get("speed"))
        if not self._require_hardware():
            self._publish_effect_result("wave", request_id, ok=False, error="hardware not initialized")
            return

        color_dict = params.get("color", {"r": 255, "g": 255, "b": 255})
        led_client.stop_effect()
        result = led_client.effect("wave", {
            "color": color_dict,
            "cycles": params.get("cycles", 1),
            "speed": params.get("speed", 0.1),
            "wait_ms": params.get("wait_ms", 10),
        })
        if not result.get("ok") and _is_ipc_failure(result.get("error")):
            self._signal_hardware_failed(result.get("error", "IPC failure"))
        if result.get("ok"):
            self._current_effect = "wave"
        self.publish_state()
        self._publish_effect_result("wave", request_id, ok=result.get("ok", True), error=result.get("error"))

    def on_cmd_effect_color_wipe(self, payload_str: str) -> None:
        try:
            request_id, params = self._parse_effect_payload(payload_str)
        except (json.JSONDecodeError, ValueError):
            self._publish_effect_result("color-wipe", "", ok=False, error="invalid JSON")
            return

        self._log.info("cmd/effect/color-wipe request_id=%s wait_ms=%s", request_id, params.get("wait_ms"))
        if not self._require_hardware():
            self._publish_effect_result("color-wipe", request_id, ok=False, error="hardware not initialized")
            return

        color_dict = params.get("color", {"r": 255, "g": 255, "b": 255})
        led_client.stop_effect()
        result = led_client.effect("color-wipe", {"color": color_dict, "wait_ms": params.get("wait_ms", 50)})
        if not result.get("ok") and _is_ipc_failure(result.get("error")):
            self._signal_hardware_failed(result.get("error", "IPC failure"))
        if result.get("ok"):
            self._current_effect = "color-wipe"
        self.publish_state()
        self._publish_effect_result("color-wipe", request_id, ok=result.get("ok", True), error=result.get("error"))

    def on_cmd_effect_color_fade(self, payload_str: str) -> None:
        try:
            request_id, params = self._parse_effect_payload(payload_str)
        except (json.JSONDecodeError, ValueError):
            self._publish_effect_result("color-fade", "", ok=False, error="invalid JSON")
            return

        self._log.info("cmd/effect/color-fade request_id=%s steps=%s", request_id, params.get("steps"))
        if not self._require_hardware():
            self._publish_effect_result("color-fade", request_id, ok=False, error="hardware not initialized")
            return

        cf = params.get("color_from", {"r": 0, "g": 0, "b": 0})
        ct = params.get("color_to", {"r": 255, "g": 255, "b": 255})
        led_client.stop_effect()
        result = led_client.effect("color-fade", {
            "color_from": cf, "color_to": ct,
            "wait_ms": params.get("wait_ms", 20),
            "steps": params.get("steps", 100),
        })
        if not result.get("ok") and _is_ipc_failure(result.get("error")):
            self._signal_hardware_failed(result.get("error", "IPC failure"))
        if result.get("ok"):
            self._current_effect = "color-fade"
        self.publish_state()
        self._publish_effect_result("color-fade", request_id, ok=result.get("ok", True), error=result.get("error"))

    def on_cmd_effect_sparkle(self, payload_str: str) -> None:
        try:
            request_id, params = self._parse_effect_payload(payload_str)
        except (json.JSONDecodeError, ValueError):
            self._publish_effect_result("sparkle", "", ok=False, error="invalid JSON")
            return

        self._log.info("cmd/effect/sparkle request_id=%s cumulative=%s", request_id, params.get("cumulative"))
        if not self._require_hardware():
            self._publish_effect_result("sparkle", request_id, ok=False, error="hardware not initialized")
            return

        color_dict = params.get("color")  # None → multicolor
        led_client.stop_effect()
        result = led_client.effect("sparkle", {
            "color": color_dict,
            "wait_ms": params.get("wait_ms", 50),
            "cumulative": params.get("cumulative", False),
        })
        if not result.get("ok") and _is_ipc_failure(result.get("error")):
            self._signal_hardware_failed(result.get("error", "IPC failure"))
        if result.get("ok"):
            self._current_effect = "sparkle"
        self.publish_state()
        self._publish_effect_result("sparkle", request_id, ok=result.get("ok", True), error=result.get("error"))

    def on_cmd_effect_rainbow(self, payload_str: str) -> None:
        try:
            request_id, params = self._parse_effect_payload(payload_str)
        except (json.JSONDecodeError, ValueError):
            self._publish_effect_result("rainbow", "", ok=False, error="invalid JSON")
            return

        self._log.info("cmd/effect/rainbow request_id=%s wait_ms=%s", request_id, params.get("wait_ms"))
        if not self._require_hardware():
            self._publish_effect_result("rainbow", request_id, ok=False, error="hardware not initialized")
            return

        led_client.stop_effect()
        result = led_client.effect("rainbow", {"wait_ms": params.get("wait_ms", 50)})
        if not result.get("ok") and _is_ipc_failure(result.get("error")):
            self._signal_hardware_failed(result.get("error", "IPC failure"))
        if result.get("ok"):
            self._current_effect = "rainbow"
        self.publish_state()
        self._publish_effect_result("rainbow", request_id, ok=result.get("ok", True), error=result.get("error"))

    def on_cmd_effect_rainbow_cycle(self, payload_str: str) -> None:
        try:
            request_id, params = self._parse_effect_payload(payload_str)
        except (json.JSONDecodeError, ValueError):
            self._publish_effect_result("rainbow-cycle", "", ok=False, error="invalid JSON")
            return

        self._log.info("cmd/effect/rainbow-cycle request_id=%s wait_ms=%s", request_id, params.get("wait_ms"))
        if not self._require_hardware():
            self._publish_effect_result("rainbow-cycle", request_id, ok=False, error="hardware not initialized")
            return

        led_client.stop_effect()
        result = led_client.effect("rainbow-cycle", {"wait_ms": params.get("wait_ms", 50)})
        if not result.get("ok") and _is_ipc_failure(result.get("error")):
            self._signal_hardware_failed(result.get("error", "IPC failure"))
        if result.get("ok"):
            self._current_effect = "rainbow-cycle"
        self.publish_state()
        self._publish_effect_result("rainbow-cycle", request_id, ok=result.get("ok", True), error=result.get("error"))

    def on_cmd_effect_theater_chase(self, payload_str: str) -> None:
        try:
            request_id, params = self._parse_effect_payload(payload_str)
        except (json.JSONDecodeError, ValueError):
            self._publish_effect_result("theater-chase", "", ok=False, error="invalid JSON")
            return

        self._log.info("cmd/effect/theater-chase request_id=%s wait_ms=%s", request_id, params.get("wait_ms"))
        if not self._require_hardware():
            self._publish_effect_result("theater-chase", request_id, ok=False, error="hardware not initialized")
            return

        color_dict = params.get("color")  # None → multicolor
        led_client.stop_effect()
        result = led_client.effect("theater-chase", {"color": color_dict, "wait_ms": params.get("wait_ms", 50)})
        if not result.get("ok") and _is_ipc_failure(result.get("error")):
            self._signal_hardware_failed(result.get("error", "IPC failure"))
        if result.get("ok"):
            self._current_effect = "theater-chase"
        self.publish_state()
        self._publish_effect_result("theater-chase", request_id, ok=result.get("ok", True), error=result.get("error"))

    def on_cmd_effect_running(self, payload_str: str) -> None:
        try:
            request_id, params = self._parse_effect_payload(payload_str)
        except (json.JSONDecodeError, ValueError):
            self._publish_effect_result("running", "", ok=False, error="invalid JSON")
            return

        self._log.info("cmd/effect/running request_id=%s wait_ms=%s width=%s", request_id, params.get("wait_ms"), params.get("width"))
        if not self._require_hardware():
            self._publish_effect_result("running", request_id, ok=False, error="hardware not initialized")
            return

        led_client.stop_effect()
        result = led_client.effect("running", {"wait_ms": params.get("wait_ms", 10), "width": params.get("width", 1)})
        if not result.get("ok") and _is_ipc_failure(result.get("error")):
            self._signal_hardware_failed(result.get("error", "IPC failure"))
        if result.get("ok"):
            self._current_effect = "running"
        self.publish_state()
        self._publish_effect_result("running", request_id, ok=result.get("ok", True), error=result.get("error"))
