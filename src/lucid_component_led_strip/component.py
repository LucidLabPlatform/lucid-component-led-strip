"""
LEDStripComponent — LUCID component for WS281x LED strip control.

Publishes retained: metadata, status, state, cfg.
Custom effect commands: cmd/effect/<name> → evt/effect/<name>/result.
Standard commands: cmd/reset, cmd/ping, cmd/cfg/set.

Hardware defaults match the OptiTrack truss installation:
  Strip 1: 896 LEDs on GPIO18   Strip 2: 894 LEDs on GPIO13
  Total: 1790 LEDs, 800 kHz, DMA 10, initial brightness 125.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from lucid_component_base import Component, ComponentContext

from .hardware import LEDStripHardware
from .effects import EffectOrchestrator
from . import effects as fx


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clamp(value: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, value))


class LEDStripComponent(Component):
    """
    LUCID component for WS281x LED strip control.

    State topics reflect live hardware state. Custom effect commands follow
    the pattern cmd/effect/<name> with JSON payloads matching effects_map
    parameters. All results are published to evt/effect/<name>/result.
    """

    # Effect names registered as LUCID capabilities
    EFFECT_CAPABILITIES = [
        "effect/clear",
        "effect/set-color",
        "effect/set-brightness",
        "effect/set-range-percent",
        "effect/set-range-exact",
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

        cfg = context.config or {}

        # Hardware configuration — defaults match the truss installation.
        self._strip1_count: int = int(cfg.get("strip1_count", 896))
        self._strip2_count: int = int(cfg.get("strip2_count", 894))
        self._strip1_pin: int = int(cfg.get("strip1_pin", 18))
        self._strip2_pin: int = int(cfg.get("strip2_pin", 13))
        self._brightness: int = _clamp(int(cfg.get("brightness", 125)), 0, 255)

        self._hardware: LEDStripHardware | None = None
        self._orchestrator = EffectOrchestrator()
        self._hardware_initialized = False

    # ------------------------------------------------------------------
    # LUCID contract
    # ------------------------------------------------------------------

    @property
    def component_id(self) -> str:
        return "led_strip"

    def capabilities(self) -> list[str]:
        return ["reset", "ping"] + self.EFFECT_CAPABILITIES

    def get_state_payload(self) -> dict[str, Any]:
        return {
            "brightness": self._brightness,
            "current_effect": self._orchestrator.current_effect,
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

    def _start(self) -> None:
        try:
            self._hardware = LEDStripHardware(
                strip1_count=self._strip1_count,
                strip2_count=self._strip2_count,
                strip1_pin=self._strip1_pin,
                strip2_pin=self._strip2_pin,
                brightness=self._brightness,
            )
            self._hardware_initialized = True
            self._log.info(
                "Hardware initialized: %d LEDs (strip1=%d GPIO%d, strip2=%d GPIO%d)",
                self._strip1_count + self._strip2_count,
                self._strip1_count, self._strip1_pin,
                self._strip2_count, self._strip2_pin,
            )
        except Exception as exc:
            self._hardware_initialized = False
            self._log.error("Hardware initialization failed: %s", exc)

        self._publish_all_retained()

    def _stop(self) -> None:
        self._orchestrator.stop()
        if self._hardware is not None:
            try:
                self._hardware.clear_all()
            except Exception:
                pass
        self._log.info("Stopped component: %s", self.component_id)

    def _publish_all_retained(self) -> None:
        self.publish_metadata()
        self.publish_status()
        self.publish_state()
        self.set_telemetry_config({"metrics": {}})
        self.publish_cfg()

    # ------------------------------------------------------------------
    # Standard commands
    # ------------------------------------------------------------------

    def on_cmd_reset(self, payload_str: str) -> None:
        try:
            payload = json.loads(payload_str) if payload_str else {}
            request_id = payload.get("request_id", "")
        except json.JSONDecodeError:
            request_id = ""

        self._orchestrator.stop()
        if self._hardware is not None:
            self._hardware.clear_all()

        self.publish_state()
        self.publish_result("reset", request_id, ok=True, error=None)

    def on_cmd_ping(self, payload_str: str) -> None:
        try:
            payload = json.loads(payload_str) if payload_str else {}
            request_id = payload.get("request_id", "")
        except json.JSONDecodeError:
            request_id = ""
        self.publish_result("ping", request_id, ok=True, error=None)

    def on_cmd_cfg_set(self, payload_str: str) -> None:
        try:
            payload = json.loads(payload_str) if payload_str else {}
            request_id = payload.get("request_id", "")
            set_dict = payload.get("set") or {}
        except json.JSONDecodeError:
            request_id = ""
            set_dict = {}

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

        if "logs_enabled" in set_dict:
            self._logs_enabled = bool(set_dict["logs_enabled"])
            applied["logs_enabled"] = self._logs_enabled

        # Runtime-applicable config
        if "brightness" in set_dict:
            self._brightness = _clamp(int(set_dict["brightness"]), 0, 255)
            applied["brightness"] = self._brightness
            if self._hardware is not None:
                self._hardware.set_brightness(self._brightness)

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

    def _require_hardware(self) -> LEDStripHardware | None:
        if self._hardware is None or not self._hardware_initialized:
            return None
        return self._hardware

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

    def on_cmd_effect_clear(self, payload_str: str) -> None:
        try:
            payload = json.loads(payload_str) if payload_str else {}
            request_id = payload.get("request_id", "")
        except json.JSONDecodeError:
            request_id = ""

        self._orchestrator.stop()
        hw = self._require_hardware()
        if hw is None:
            self._publish_effect_result("clear", request_id, ok=False, error="hardware not initialized")
            return
        hw.clear_all()
        self.publish_state()
        self._publish_effect_result("clear", request_id, ok=True)

    def on_cmd_effect_set_color(self, payload_str: str) -> None:
        try:
            request_id, params = self._parse_effect_payload(payload_str)
        except (json.JSONDecodeError, ValueError):
            self._publish_effect_result("set-color", "", ok=False, error="invalid JSON")
            return

        hw = self._require_hardware()
        if hw is None:
            self._publish_effect_result("set-color", request_id, ok=False, error="hardware not initialized")
            return

        self._orchestrator.stop()
        color_dict = params.get("color")
        if color_dict:
            from rpi_ws281x import Color
            hw.set_color_all(Color(int(color_dict["r"]), int(color_dict["g"]), int(color_dict["b"])))
        else:
            hw.set_white_all()

        self.publish_state()
        self._publish_effect_result("set-color", request_id, ok=True)

    def on_cmd_effect_set_brightness(self, payload_str: str) -> None:
        try:
            request_id, params = self._parse_effect_payload(payload_str)
        except (json.JSONDecodeError, ValueError):
            self._publish_effect_result("set-brightness", "", ok=False, error="invalid JSON")
            return

        hw = self._require_hardware()
        if hw is None:
            self._publish_effect_result("set-brightness", request_id, ok=False, error="hardware not initialized")
            return

        brightness = _clamp(int(params.get("brightness", 125)), 0, 255)
        self._brightness = brightness
        hw.set_brightness(brightness)
        self.publish_state()
        self._publish_effect_result("set-brightness", request_id, ok=True)

    def on_cmd_effect_set_range_percent(self, payload_str: str) -> None:
        try:
            request_id, params = self._parse_effect_payload(payload_str)
        except (json.JSONDecodeError, ValueError):
            self._publish_effect_result("set-range-percent", "", ok=False, error="invalid JSON")
            return

        hw = self._require_hardware()
        if hw is None:
            self._publish_effect_result("set-range-percent", request_id, ok=False, error="hardware not initialized")
            return

        self._orchestrator.stop()
        from rpi_ws281x import Color
        color_dict = params.get("color", {"r": 255, "g": 255, "b": 255})
        hw.set_color_range_percent(
            Color(int(color_dict["r"]), int(color_dict["g"]), int(color_dict["b"])),
            float(params.get("start_percent", 0.0)),
            float(params.get("end_percent", 1.0)),
        )
        self.publish_state()
        self._publish_effect_result("set-range-percent", request_id, ok=True)

    def on_cmd_effect_set_range_exact(self, payload_str: str) -> None:
        try:
            request_id, params = self._parse_effect_payload(payload_str)
        except (json.JSONDecodeError, ValueError):
            self._publish_effect_result("set-range-exact", "", ok=False, error="invalid JSON")
            return

        hw = self._require_hardware()
        if hw is None:
            self._publish_effect_result("set-range-exact", request_id, ok=False, error="hardware not initialized")
            return

        self._orchestrator.stop()
        from rpi_ws281x import Color
        color_dict = params.get("color", {"r": 255, "g": 255, "b": 255})
        hw.set_color_range_exact(
            Color(int(color_dict["r"]), int(color_dict["g"]), int(color_dict["b"])),
            int(params.get("start_index", 0)),
            int(params.get("end_index", hw.LED_COUNT)),
        )
        self.publish_state()
        self._publish_effect_result("set-range-exact", request_id, ok=True)

    def on_cmd_effect_glow(self, payload_str: str) -> None:
        try:
            request_id, params = self._parse_effect_payload(payload_str)
        except (json.JSONDecodeError, ValueError):
            self._publish_effect_result("glow", "", ok=False, error="invalid JSON")
            return

        hw = self._require_hardware()
        if hw is None:
            self._publish_effect_result("glow", request_id, ok=False, error="hardware not initialized")
            return

        color_dict = params.get("color", {"r": 255, "g": 255, "b": 255})
        self._orchestrator.start(
            "glow", fx.glow.run, hw,
            r=int(color_dict["r"]),
            g=int(color_dict["g"]),
            b=int(color_dict["b"]),
            wait_ms=int(params.get("wait_ms", 10)),
        )
        self.publish_state()
        self._publish_effect_result("glow", request_id, ok=True)

    def on_cmd_effect_wave(self, payload_str: str) -> None:
        try:
            request_id, params = self._parse_effect_payload(payload_str)
        except (json.JSONDecodeError, ValueError):
            self._publish_effect_result("wave", "", ok=False, error="invalid JSON")
            return

        hw = self._require_hardware()
        if hw is None:
            self._publish_effect_result("wave", request_id, ok=False, error="hardware not initialized")
            return

        color_dict = params.get("color", {"r": 255, "g": 255, "b": 255})
        self._orchestrator.start(
            "wave", fx.wave.run, hw,
            r=int(color_dict["r"]),
            g=int(color_dict["g"]),
            b=int(color_dict["b"]),
            cycles=int(params.get("cycles", 1)),
            speed=float(params.get("speed", 0.1)),
            wait_ms=int(params.get("wait_ms", 10)),
        )
        self.publish_state()
        self._publish_effect_result("wave", request_id, ok=True)

    def on_cmd_effect_color_wipe(self, payload_str: str) -> None:
        try:
            request_id, params = self._parse_effect_payload(payload_str)
        except (json.JSONDecodeError, ValueError):
            self._publish_effect_result("color-wipe", "", ok=False, error="invalid JSON")
            return

        hw = self._require_hardware()
        if hw is None:
            self._publish_effect_result("color-wipe", request_id, ok=False, error="hardware not initialized")
            return

        color_dict = params.get("color", {"r": 255, "g": 255, "b": 255})
        self._orchestrator.start(
            "color-wipe", fx.color_wipe.run, hw,
            r=int(color_dict["r"]),
            g=int(color_dict["g"]),
            b=int(color_dict["b"]),
            wait_ms=int(params.get("wait_ms", 50)),
        )
        self.publish_state()
        self._publish_effect_result("color-wipe", request_id, ok=True)

    def on_cmd_effect_color_fade(self, payload_str: str) -> None:
        try:
            request_id, params = self._parse_effect_payload(payload_str)
        except (json.JSONDecodeError, ValueError):
            self._publish_effect_result("color-fade", "", ok=False, error="invalid JSON")
            return

        hw = self._require_hardware()
        if hw is None:
            self._publish_effect_result("color-fade", request_id, ok=False, error="hardware not initialized")
            return

        cf = params.get("color_from", {"r": 0, "g": 0, "b": 0})
        ct = params.get("color_to", {"r": 255, "g": 255, "b": 255})
        self._orchestrator.start(
            "color-fade", fx.color_fade.run, hw,
            r_from=int(cf["r"]), g_from=int(cf["g"]), b_from=int(cf["b"]),
            r_to=int(ct["r"]), g_to=int(ct["g"]), b_to=int(ct["b"]),
            wait_ms=int(params.get("wait_ms", 20)),
            steps=int(params.get("steps", 100)),
        )
        self.publish_state()
        self._publish_effect_result("color-fade", request_id, ok=True)

    def on_cmd_effect_sparkle(self, payload_str: str) -> None:
        try:
            request_id, params = self._parse_effect_payload(payload_str)
        except (json.JSONDecodeError, ValueError):
            self._publish_effect_result("sparkle", "", ok=False, error="invalid JSON")
            return

        hw = self._require_hardware()
        if hw is None:
            self._publish_effect_result("sparkle", request_id, ok=False, error="hardware not initialized")
            return

        color_dict = params.get("color")
        # color=null → multicolor (sentinel -1)
        r = int(color_dict["r"]) if color_dict else -1
        g = int(color_dict["g"]) if color_dict else -1
        b = int(color_dict["b"]) if color_dict else -1

        self._orchestrator.start(
            "sparkle", fx.sparkle.run, hw,
            r=r, g=g, b=b,
            wait_ms=int(params.get("wait_ms", 50)),
            cumulative=bool(params.get("cumulative", False)),
        )
        self.publish_state()
        self._publish_effect_result("sparkle", request_id, ok=True)

    def on_cmd_effect_rainbow(self, payload_str: str) -> None:
        try:
            request_id, params = self._parse_effect_payload(payload_str)
        except (json.JSONDecodeError, ValueError):
            self._publish_effect_result("rainbow", "", ok=False, error="invalid JSON")
            return

        hw = self._require_hardware()
        if hw is None:
            self._publish_effect_result("rainbow", request_id, ok=False, error="hardware not initialized")
            return

        self._orchestrator.start(
            "rainbow", fx.rainbow.run, hw,
            wait_ms=int(params.get("wait_ms", 50)),
        )
        self.publish_state()
        self._publish_effect_result("rainbow", request_id, ok=True)

    def on_cmd_effect_rainbow_cycle(self, payload_str: str) -> None:
        try:
            request_id, params = self._parse_effect_payload(payload_str)
        except (json.JSONDecodeError, ValueError):
            self._publish_effect_result("rainbow-cycle", "", ok=False, error="invalid JSON")
            return

        hw = self._require_hardware()
        if hw is None:
            self._publish_effect_result("rainbow-cycle", request_id, ok=False, error="hardware not initialized")
            return

        self._orchestrator.start(
            "rainbow-cycle", fx.rainbow_cycle.run, hw,
            wait_ms=int(params.get("wait_ms", 50)),
        )
        self.publish_state()
        self._publish_effect_result("rainbow-cycle", request_id, ok=True)

    def on_cmd_effect_theater_chase(self, payload_str: str) -> None:
        try:
            request_id, params = self._parse_effect_payload(payload_str)
        except (json.JSONDecodeError, ValueError):
            self._publish_effect_result("theater-chase", "", ok=False, error="invalid JSON")
            return

        hw = self._require_hardware()
        if hw is None:
            self._publish_effect_result("theater-chase", request_id, ok=False, error="hardware not initialized")
            return

        color_dict = params.get("color")
        r = int(color_dict["r"]) if color_dict else -1
        g = int(color_dict["g"]) if color_dict else -1
        b = int(color_dict["b"]) if color_dict else -1

        self._orchestrator.start(
            "theater-chase", fx.theater_chase.run, hw,
            r=r, g=g, b=b,
            wait_ms=int(params.get("wait_ms", 50)),
        )
        self.publish_state()
        self._publish_effect_result("theater-chase", request_id, ok=True)

    def on_cmd_effect_running(self, payload_str: str) -> None:
        try:
            request_id, params = self._parse_effect_payload(payload_str)
        except (json.JSONDecodeError, ValueError):
            self._publish_effect_result("running", "", ok=False, error="invalid JSON")
            return

        hw = self._require_hardware()
        if hw is None:
            self._publish_effect_result("running", request_id, ok=False, error="hardware not initialized")
            return

        self._orchestrator.start(
            "running", fx.running.run, hw,
            wait_ms=int(params.get("wait_ms", 10)),
            width=int(params.get("width", 1)),
        )
        self.publish_state()
        self._publish_effect_result("running", request_id, ok=True)
