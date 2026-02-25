"""
Contract tests for LEDStripComponent.

These tests run without real hardware by mocking Adafruit_NeoPixel before
any import that would pull in rpi_ws281x. They verify the LUCID lifecycle
contract, state payload structure, and capabilities — not LED output.
"""
from __future__ import annotations

import sys
import types
import threading
from unittest.mock import MagicMock, patch

import pytest
from lucid_component_base import ComponentContext, ComponentStatus


# ---------------------------------------------------------------------------
# Stub rpi_ws281x before importing the component so tests run off-Pi.
# ---------------------------------------------------------------------------

def _stub_rpi_ws281x():
    """Install a minimal fake rpi_ws281x module into sys.modules."""
    if "rpi_ws281x" in sys.modules:
        return

    mod = types.ModuleType("rpi_ws281x")

    class _Color(int):
        def __new__(cls, r=0, g=0, b=0):
            value = (r << 16) | (g << 8) | b
            obj = super().__new__(cls, value)
            obj.r, obj.g, obj.b = r, g, b
            return obj

    class _NeoPixel:
        def __init__(self, *args, **kwargs):
            pass
        def begin(self):
            pass
        def setPixelColor(self, idx, color):
            pass
        def show(self):
            pass
        def setBrightness(self, b):
            pass

    mod.Color = _Color
    mod.Adafruit_NeoPixel = _NeoPixel
    sys.modules["rpi_ws281x"] = mod


_stub_rpi_ws281x()

from lucid_component_led_strip import LEDStripComponent  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fake_context(component_id: str = "led_strip") -> ComponentContext:
    class FakeMqtt:
        def publish(self, topic: str, payload, *, qos: int = 0, retain: bool = False) -> None:
            pass

    return ComponentContext.create(
        agent_id="test-agent",
        base_topic="lucid/agents/test-agent",
        component_id=component_id,
        mqtt=FakeMqtt(),
        config={},
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_component_instantiation():
    ctx = _fake_context()
    comp = LEDStripComponent(ctx)
    assert comp.component_id == "led_strip"
    assert comp.state.status == ComponentStatus.STOPPED


def test_capabilities_includes_standard_and_effects():
    ctx = _fake_context()
    comp = LEDStripComponent(ctx)
    caps = comp.capabilities()

    assert isinstance(caps, list)
    assert "reset" in caps
    assert "ping" in caps

    expected_effects = [
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
    for effect in expected_effects:
        assert effect in caps, f"Missing capability: {effect}"


def test_get_state_payload_structure():
    ctx = _fake_context()
    comp = LEDStripComponent(ctx)
    state = comp.get_state_payload()

    assert isinstance(state, dict)
    required_keys = [
        "brightness",
        "current_effect",
        "led_count",
        "strip1_count",
        "strip2_count",
        "strip1_pin",
        "strip2_pin",
        "hardware_initialized",
    ]
    for key in required_keys:
        assert key in state, f"Missing state key: {key}"

    assert state["led_count"] == state["strip1_count"] + state["strip2_count"]
    assert 0 <= state["brightness"] <= 255
    assert isinstance(state["hardware_initialized"], bool)


def test_default_hardware_config():
    ctx = _fake_context()
    comp = LEDStripComponent(ctx)
    state = comp.get_state_payload()

    assert state["strip1_count"] == 896
    assert state["strip2_count"] == 894
    assert state["led_count"] == 1790
    assert state["strip1_pin"] == 18
    assert state["strip2_pin"] == 13
    assert state["brightness"] == 125


def test_start_stop_state_transitions():
    ctx = _fake_context()
    comp = LEDStripComponent(ctx)

    with patch("lucid_component_led_strip.component.led_client") as mock_client:
        mock_client.init.return_value = {"ok": True}
        comp.start()
        assert comp.state.status == ComponentStatus.RUNNING
        assert comp.state.started_at is not None

        comp.stop()
        assert comp.state.status == ComponentStatus.STOPPED
        assert comp.state.stopped_at is not None


def test_hardware_initialized_after_start():
    ctx = _fake_context()
    comp = LEDStripComponent(ctx)
    with patch("lucid_component_led_strip.component.led_client") as mock_client:
        mock_client.init.return_value = {"ok": True}
        comp.start()
        state = comp.get_state_payload()
        assert state["hardware_initialized"] is True
    comp.stop()


def test_on_cmd_ping():
    import json
    ctx = _fake_context()
    comp = LEDStripComponent(ctx)
    with patch("lucid_component_led_strip.component.led_client") as mock_client:
        mock_client.init.return_value = {"ok": True}
        comp.start()

        published = []
        comp.context.mqtt.publish = lambda t, p, **kw: published.append((t, p))

        comp.on_cmd_ping(json.dumps({"request_id": "ping-001"}))

        result_topics = [t for t, _ in published if "evt/ping/result" in t]
        assert result_topics, "Expected ping result to be published"

        comp.stop()


def test_on_cmd_clear():
    import json
    ctx = _fake_context()
    comp = LEDStripComponent(ctx)
    with patch("lucid_component_led_strip.component.led_client") as mock_client:
        mock_client.clear.return_value = {"ok": True}
        comp._hardware_initialized = True

        published = []
        comp.context.mqtt.publish = lambda t, p, **kw: published.append((t, p))

        comp.on_cmd_clear(json.dumps({"request_id": "clear-001"}))

        result_topics = [t for t, _ in published if "evt/clear/result" in t]
        assert result_topics, "Expected clear result on evt/clear/result"
        results = [json.loads(p) for t, p in published if "evt/clear/result" in t]
        assert results[0]["ok"] is True
        assert results[0]["request_id"] == "clear-001"
        assert comp._current_effect is None


def test_on_cmd_clear_without_hardware_publishes_error_result():
    import json
    ctx = _fake_context()
    comp = LEDStripComponent(ctx)
    comp._hardware_initialized = False

    published = []
    comp.context.mqtt.publish = lambda t, p, **kw: published.append((t, p))

    comp.on_cmd_clear(json.dumps({"request_id": "clear-002"}))

    results = [json.loads(p) for t, p in published if "evt/clear/result" in t]
    assert results, "Expected clear result on evt/clear/result"
    assert results[0]["ok"] is False
    assert results[0]["request_id"] == "clear-002"
    assert results[0]["error"] == "hardware not initialized"


def test_on_cmd_reset():
    import json
    ctx = _fake_context()
    comp = LEDStripComponent(ctx)
    with patch("lucid_component_led_strip.component.led_client") as mock_client:
        mock_client.init.return_value = {"ok": True}
        mock_client.reset.return_value = {"ok": True}
        comp.start()

        published = []
        comp.context.mqtt.publish = lambda t, p, **kw: published.append((t, p))

        comp.on_cmd_reset(json.dumps({"request_id": "reset-001"}))

        result_topics = [t for t, _ in published if "evt/reset/result" in t]
        assert result_topics, "Expected reset result to be published"
        assert comp._current_effect is None

        comp.stop()


def test_on_cmd_cfg_set_brightness():
    import json
    ctx = _fake_context()
    comp = LEDStripComponent(ctx)
    with patch("lucid_component_led_strip.component.led_client") as mock_client:
        mock_client.init.return_value = {"ok": True}
        mock_client.set_brightness.return_value = {"ok": True}
        comp.start()

        comp.on_cmd_cfg_set(json.dumps({
            "request_id": "cfg-001",
            "set": {"brightness": 200},
        }))

        assert comp._brightness == 200
        state = comp.get_state_payload()
        assert state["brightness"] == 200

        comp.stop()


def test_on_cmd_cfg_set_invalid_json():
    ctx = _fake_context()
    comp = LEDStripComponent(ctx)
    with patch("lucid_component_led_strip.component.led_client") as mock_client:
        mock_client.init.return_value = {"ok": True}
        comp.start()

        published = []
        comp.context.mqtt.publish = lambda t, p, **kw: published.append((t, p))

        comp.on_cmd_cfg_set("not json {{{")

        comp.stop()


def test_effect_result_published_when_hardware_ready():
    import json
    ctx = _fake_context()
    comp = LEDStripComponent(ctx)
    with patch("lucid_component_led_strip.component.led_client") as mock_client:
        mock_client.init.return_value = {"ok": True}
        mock_client.effect.return_value = {"ok": True}
        comp.start()

        published = []
        comp.context.mqtt.publish = lambda t, p, **kw: published.append((t, p))

        comp.on_cmd_effect_rainbow(json.dumps({"request_id": "fx-001", "wait_ms": 50}))

        result_topics = [t for t, _ in published if "evt/effect/rainbow/result" in t]
        assert result_topics, "Expected rainbow effect result to be published"

        results = [json.loads(p) for t, p in published if "evt/effect/rainbow/result" in t]
        assert results[0]["ok"] is True
        assert results[0]["request_id"] == "fx-001"

    comp.stop()


def test_orchestrator_tracks_current_effect():
    import json
    ctx = _fake_context()
    comp = LEDStripComponent(ctx)
    with patch("lucid_component_led_strip.component.led_client") as mock_client:
        mock_client.init.return_value = {"ok": True}
        mock_client.effect.return_value = {"ok": True}
        mock_client.reset.return_value = {"ok": True}
        comp.start()

        comp.on_cmd_effect_glow(json.dumps({
            "request_id": "fx-002",
            "color": {"r": 0, "g": 255, "b": 0},
            "wait_ms": 10,
        }))

        assert comp._current_effect == "glow"

        comp.on_cmd_reset(json.dumps({"request_id": "r-001"}))
        assert comp._current_effect is None

    comp.stop()
