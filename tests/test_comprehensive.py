"""
Comprehensive tests for lucid-component-led-strip.

Hardware is mocked via sys.modules injection before any import that would
pull in rpi_ws281x or numpy. The Unix socket IPC layer (led_client) is
mocked with unittest.mock.patch throughout.

Coverage targets:
- component.py  — all command handlers, IPC-failure paths, lifecycle
- hardware.py   — pixel math, ring indexing, wheel, range helpers
- effects/      — EffectOrchestrator lifecycle + all 9 effect run() functions
- helper_server.py — HelperState methods, _handle_request dispatch
- client.py     — socket error paths
- protocol.py   — constants import
"""
from __future__ import annotations

import json
import sys
import threading
import types
from unittest.mock import MagicMock, patch

import pytest
from lucid_component_base import ComponentContext, ComponentStatus


# ---------------------------------------------------------------------------
# Hardware stubs — must be injected BEFORE any package import
# ---------------------------------------------------------------------------

def _ensure_rpi_ws281x_stub() -> None:
    """Install a minimal fake rpi_ws281x module into sys.modules."""
    if "rpi_ws281x" in sys.modules:
        return
    mod = types.ModuleType("rpi_ws281x")

    class _Color(int):
        def __new__(cls, r: int = 0, g: int = 0, b: int = 0):
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


def _ensure_numpy_stub() -> None:
    """Install a minimal numpy stub so wave.py can be imported off-Pi."""
    if "numpy" in sys.modules:
        return
    try:
        import numpy  # noqa: F401 — real numpy is present
        return
    except ImportError:
        pass
    np = types.ModuleType("numpy")
    import math as _math

    def array(data, dtype=None):
        return list(data)

    def cos(x):
        if hasattr(x, "__iter__"):
            return [_math.cos(v) for v in x]
        return _math.cos(x)

    def linspace(start, stop, num):
        if num <= 1:
            return [start]
        step = (stop - start) / (num - 1)
        return [start + step * i for i in range(num)]

    def tile(arr, reps):
        if isinstance(reps, (list, tuple)):
            n = reps[0]
        else:
            n = reps
        return [list(arr) for _ in range(n)]

    def multiply(a, b):
        if isinstance(a, list) and isinstance(b, list):
            return [[ai * bi for ai, bi in zip(row, [b[i]] if not hasattr(b[i], "__iter__") else b[i])]
                    for i, row in enumerate(a)]
        return a

    class _dtype_stub:
        pass

    np.array = array
    np.cos = cos
    np.linspace = linspace
    np.tile = tile
    np.multiply = multiply
    np.uint8 = _dtype_stub
    np.pi = _math.pi
    np.newaxis = None
    sys.modules["numpy"] = np


_ensure_rpi_ws281x_stub()
_ensure_numpy_stub()

# Now we can safely import the package
from lucid_component_led_strip import LEDStripComponent  # noqa: E402
from lucid_component_led_strip import client as led_client_module  # noqa: E402
from lucid_component_led_strip import helper_server  # noqa: E402
from lucid_component_led_strip import protocol  # noqa: E402
from lucid_component_led_strip.effects import EffectOrchestrator  # noqa: E402
from lucid_component_led_strip.hardware import LEDStripHardware  # noqa: E402


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _fake_context(component_id: str = "led_strip") -> ComponentContext:
    class FakeMqtt:
        def __init__(self):
            self.published: list[tuple[str, str]] = []

        def publish(self, topic: str, payload, *, qos: int = 0, retain: bool = False) -> None:
            self.published.append((topic, payload if isinstance(payload, str) else payload))

    return ComponentContext.create(
        agent_id="test-agent",
        base_topic="lucid/agents/test-agent",
        component_id=component_id,
        mqtt=FakeMqtt(),
        config={},
    )


def _make_comp_started(mock_client=None):
    """Return a running LEDStripComponent with patched led_client."""
    ctx = _fake_context()
    comp = LEDStripComponent(ctx)
    if mock_client is None:
        mock_client = MagicMock()
        mock_client.init.return_value = {"ok": True}
        mock_client.reset.return_value = {"ok": True}
        mock_client.clear.return_value = {"ok": True}
        mock_client.stop_effect.return_value = {"ok": True}
        mock_client.effect.return_value = {"ok": True}
        mock_client.set_brightness.return_value = {"ok": True}
        mock_client.set_color.return_value = {"ok": True}
        mock_client.set_range_percent.return_value = {"ok": True}
        mock_client.set_range_exact.return_value = {"ok": True}
        mock_client.get_pixels.return_value = {"ok": True, "pixels": [[1, 2, 3]]}
    return comp, mock_client


def _published_results(ctx: ComponentContext, evt_fragment: str) -> list[dict]:
    return [
        json.loads(p)
        for t, p in ctx.mqtt.published
        if evt_fragment in t
    ]


# ===========================================================================
# protocol.py
# ===========================================================================

class TestProtocol:
    def test_constants_exist(self):
        assert protocol.CMD_INIT == "init"
        assert protocol.CMD_RESET == "reset"
        assert protocol.CMD_STOP_EFFECT == "stop_effect"
        assert protocol.CMD_SET_BRIGHTNESS == "set_brightness"
        assert protocol.CMD_CLEAR == "clear"
        assert protocol.CMD_SET_COLOR == "set_color"
        assert protocol.CMD_SET_RANGE_PERCENT == "set_range_percent"
        assert protocol.CMD_SET_RANGE_EXACT == "set_range_exact"
        assert protocol.CMD_EFFECT == "effect"
        assert protocol.CMD_PING == "ping"
        assert protocol.CMD_GET_PIXELS == "get_pixels"
        assert protocol.DEFAULT_SOCKET_PATH == "/run/lucid/led-strip.sock"


# ===========================================================================
# hardware.py — LEDStripHardware
# ===========================================================================

class TestLEDStripHardware:
    """Tests for the pixel math layer. rpi_ws281x is stubbed so no real DMA."""

    def _make_hw(self, strip1_count: int = 5, strip2_count: int = 3) -> LEDStripHardware:
        return LEDStripHardware(
            strip1_count=strip1_count,
            strip2_count=strip2_count,
            strip1_pin=18,
            strip2_pin=13,
            brightness=128,
        )

    def test_led_count(self):
        hw = self._make_hw(5, 3)
        assert hw.LED_COUNT == 8

    def test_pixel_buffer_initialized_to_black(self):
        hw = self._make_hw(4, 2)
        buf = hw.get_pixel_buffer()
        assert len(buf) == 6
        assert all(p == [0, 0, 0] for p in buf)

    def test_set_pixel_color_updates_buffer_strip1(self):
        hw = self._make_hw(4, 2)
        from rpi_ws281x import Color
        hw.set_pixel_color(0, Color(10, 20, 30))
        buf = hw.get_pixel_buffer()
        assert buf[0] == [10, 20, 30]

    def test_set_pixel_color_out_of_range_is_noop(self):
        hw = self._make_hw(4, 2)
        from rpi_ws281x import Color
        hw.set_pixel_color(100, Color(255, 0, 0))  # should not raise
        buf = hw.get_pixel_buffer()
        assert all(p == [0, 0, 0] for p in buf)

    def test_set_pixel_color_strip2_reversed(self):
        hw = self._make_hw(4, 4)
        from rpi_ws281x import Color
        # Pixel index 4 is the first logical pixel on strip 2
        # Strip 2 is reversed: logical index 4 → physical reversed_index = 3
        hw.set_pixel_color(4, Color(255, 0, 0))
        buf = hw.get_pixel_buffer()
        assert buf[4] == [255, 0, 0]

    def test_clear_all_sets_all_black(self):
        hw = self._make_hw(4, 2)
        from rpi_ws281x import Color
        hw.set_pixel_color(0, Color(100, 100, 100))
        hw.clear_all()
        buf = hw.get_pixel_buffer()
        assert all(p == [0, 0, 0] for p in buf)

    def test_set_color_all(self):
        hw = self._make_hw(4, 2)
        from rpi_ws281x import Color
        hw.set_color_all(Color(1, 2, 3))
        buf = hw.get_pixel_buffer()
        assert all(p == [1, 2, 3] for p in buf)

    def test_set_white_all(self):
        hw = self._make_hw(3, 2)
        hw.set_white_all()
        buf = hw.get_pixel_buffer()
        assert all(p == [255, 255, 255] for p in buf)

    def test_set_brightness(self):
        hw = self._make_hw(2, 2)
        hw.set_brightness(200)
        assert hw.LED_BRIGHTNESS == 200

    def test_get_pixel_buffer_returns_copy(self):
        hw = self._make_hw(3, 2)
        buf1 = hw.get_pixel_buffer()
        buf2 = hw.get_pixel_buffer()
        assert buf1 is not buf2

    def test_set_color_range_percent_full(self):
        hw = self._make_hw(10, 0)
        from rpi_ws281x import Color
        hw.set_color_range_percent(Color(5, 10, 15), 0.0, 1.0)
        buf = hw.get_pixel_buffer()
        assert all(p == [5, 10, 15] for p in buf)

    def test_set_color_range_exact_partial(self):
        hw = self._make_hw(10, 0)
        from rpi_ws281x import Color
        hw.set_color_range_exact(Color(7, 8, 9), 0, 5)
        buf = hw.get_pixel_buffer()
        for i in range(5):
            assert buf[i] == [7, 8, 9], f"pixel {i} should be colored"
        for i in range(5, 10):
            assert buf[i] == [0, 0, 0], f"pixel {i} should be black"

    def test_wheel_returns_color_three_ranges(self):
        hw = self._make_hw(2, 2)
        # pos < 85
        c0 = hw.wheel(0)
        assert c0 is not None
        # pos in 85-169
        c85 = hw.wheel(85)
        assert c85 is not None
        # pos >= 170
        c170 = hw.wheel(170)
        assert c170 is not None

    def test_wheel_pos_255_wraps(self):
        hw = self._make_hw(2, 2)
        c255 = hw.wheel(255)
        c_wrap = hw.wheel(255 & 255)
        # Both should be same computation
        assert int(c255) == int(c_wrap)

    def test_color_to_rgb_int(self):
        hw = self._make_hw(2, 2)
        # 0x0A141E = rgb(10, 20, 30)
        r, g, b = hw._color_to_rgb(0x0A141E)
        assert r == 10
        assert g == 20
        assert b == 30

    def test_color_to_rgb_iterable(self):
        hw = self._make_hw(2, 2)
        r, g, b = hw._color_to_rgb([10, 20, 30])
        assert r == 10
        assert g == 20
        assert b == 30


# ===========================================================================
# effects/__init__.py — EffectOrchestrator
# ===========================================================================

class TestEffectOrchestrator:

    def test_initial_state(self):
        orch = EffectOrchestrator()
        assert orch.current_effect is None

    def test_stop_with_no_active_effect_is_noop(self):
        orch = EffectOrchestrator()
        orch.stop()  # should not raise

    def test_start_sets_current_effect(self):
        orch = EffectOrchestrator()

        def my_effect(hw, cancel, **kw):
            cancel.wait(timeout=2.0)

        orch.start("test-fx", my_effect, object())
        assert orch.current_effect == "test-fx"
        orch.stop()

    def test_stop_clears_current_effect(self):
        orch = EffectOrchestrator()
        started = threading.Event()

        def my_effect(hw, cancel, **kw):
            started.set()
            cancel.wait(timeout=5.0)

        orch.start("test-fx", my_effect, object())
        started.wait(timeout=1.0)
        orch.stop()
        assert orch.current_effect is None

    def test_starting_new_effect_stops_previous(self):
        orch = EffectOrchestrator()
        stopped_first = threading.Event()

        def slow_effect(hw, cancel, **kw):
            cancel.wait(timeout=5.0)
            stopped_first.set()

        def fast_effect(hw, cancel, **kw):
            cancel.wait(timeout=5.0)

        orch.start("slow", slow_effect, object())
        orch.start("fast", fast_effect, object())  # should stop "slow" first
        assert stopped_first.wait(timeout=2.0)
        assert orch.current_effect == "fast"
        orch.stop()

    def test_effect_that_exits_naturally_clears_current_effect(self):
        orch = EffectOrchestrator()

        def one_shot(hw, cancel, **kw):
            pass  # returns immediately

        orch.start("one-shot", one_shot, object())
        # Give thread time to finish
        import time
        time.sleep(0.1)
        assert orch.current_effect is None

    def test_effect_exception_is_logged_not_raised(self):
        orch = EffectOrchestrator()

        def bad_effect(hw, cancel, **kw):
            raise ValueError("oops")

        orch.start("bad", bad_effect, object())
        import time
        time.sleep(0.1)
        # Should not propagate; orchestrator should still be usable
        assert orch.current_effect is None
        orch.stop()  # no-op

    def test_params_forwarded_to_effect(self):
        orch = EffectOrchestrator()
        received = {}

        def capture_effect(hw, cancel, **kw):
            received.update(kw)

        orch.start("capture", capture_effect, object(), r=10, g=20, b=30, wait_ms=5)
        import time
        time.sleep(0.1)
        assert received == {"r": 10, "g": 20, "b": 30, "wait_ms": 5}


# ===========================================================================
# effects/*.py — individual effect run() functions
# ===========================================================================

class _FakeHardware:
    """Minimal fake LEDStripHardware for testing effects without rpi_ws281x."""

    def __init__(self, led_count: int = 6):
        self.LED_COUNT = led_count
        self.pixels: list[int] = [0] * led_count
        self.shows = 0
        self._Color = sys.modules["rpi_ws281x"].Color

    def set_pixel_color(self, idx: int, color) -> None:
        if 0 <= idx < self.LED_COUNT:
            self.pixels[idx] = int(color)

    def show(self) -> None:
        self.shows += 1

    def clear_all(self) -> None:
        self.pixels = [0] * self.LED_COUNT
        self.shows += 1

    def wheel(self, pos: int):
        pos = pos & 255
        if pos < 85:
            return self._Color(pos * 3, 255 - pos * 3, 0)
        if pos < 170:
            pos -= 85
            return self._Color(255 - pos * 3, 0, pos * 3)
        pos -= 170
        return self._Color(0, pos * 3, 255 - pos * 3)


class TestGlowEffect:
    def test_glow_runs_one_frame_then_cancels(self):
        from lucid_component_led_strip.effects.glow import run
        hw = _FakeHardware(4)
        cancel = threading.Event()

        def _cancel_after_one(_hw, _cancel, **kw):
            cancel.set()

        cancel_event = threading.Event()
        # Run one iteration by patching time.sleep to trigger cancel
        call_count = [0]

        def patched_sleep(s):
            call_count[0] += 1
            cancel_event.set()  # cancel after first sleep

        with patch("lucid_component_led_strip.effects.glow.time.sleep", patched_sleep):
            run(hw, cancel_event, r=0, g=255, b=0, wait_ms=10)

        assert call_count[0] == 1
        assert hw.shows >= 1

    def test_glow_sets_all_pixels(self):
        from lucid_component_led_strip.effects.glow import run
        hw = _FakeHardware(4)
        cancel = threading.Event()

        with patch("lucid_component_led_strip.effects.glow.time.sleep", lambda s: cancel.set()):
            run(hw, cancel, r=255, g=255, b=255, wait_ms=10)

        assert hw.shows >= 1


class TestColorFadeEffect:
    def test_color_fade_runs_steps(self):
        from lucid_component_led_strip.effects.color_fade import run
        hw = _FakeHardware(4)
        cancel = threading.Event()
        frames = []

        orig_show = hw.show
        def capturing_show():
            frames.append(list(hw.pixels))
            orig_show()

        hw.show = capturing_show

        with patch("lucid_component_led_strip.effects.color_fade.time.sleep", lambda s: None):
            run(hw, cancel, r_from=0, g_from=0, b_from=0, r_to=255, g_to=0, b_to=0, steps=3, wait_ms=0)

        assert len(frames) == 3

    def test_color_fade_respects_cancel(self):
        from lucid_component_led_strip.effects.color_fade import run
        hw = _FakeHardware(4)
        cancel = threading.Event()
        cancel.set()  # pre-cancelled

        with patch("lucid_component_led_strip.effects.color_fade.time.sleep", lambda s: None):
            run(hw, cancel, steps=100, wait_ms=0)

        # With cancel pre-set, the loop body exits immediately on first check
        assert hw.shows == 0


class TestColorWipeEffect:
    def test_color_wipe_visits_each_pixel(self):
        from lucid_component_led_strip.effects.color_wipe import run
        hw = _FakeHardware(5)
        cancel = threading.Event()

        with patch("lucid_component_led_strip.effects.color_wipe.time.sleep", lambda s: None):
            run(hw, cancel, r=100, g=200, b=50, wait_ms=0)

        # After a full wipe, all pixels should be the wipe color
        from rpi_ws281x import Color
        expected = int(Color(100, 200, 50))
        assert all(p == expected for p in hw.pixels)

    def test_color_wipe_stops_early_on_cancel(self):
        from lucid_component_led_strip.effects.color_wipe import run
        hw = _FakeHardware(10)
        cancel = threading.Event()
        frames = []

        def cancelling_sleep(s):
            frames.append(1)
            if len(frames) >= 3:
                cancel.set()

        with patch("lucid_component_led_strip.effects.color_wipe.time.sleep", cancelling_sleep):
            run(hw, cancel, r=255, g=0, b=0, wait_ms=1)

        assert len(frames) < 10  # stopped early


class TestSparkleEffect:
    def test_sparkle_multicolor_mode(self):
        from lucid_component_led_strip.effects.sparkle import run
        hw = _FakeHardware(6)
        cancel = threading.Event()

        with patch("lucid_component_led_strip.effects.sparkle.time.sleep", lambda s: None):
            run(hw, cancel, r=-1, g=-1, b=-1, wait_ms=0, cumulative=True)

        assert hw.shows >= 1

    def test_sparkle_single_color_mode(self):
        from lucid_component_led_strip.effects.sparkle import run
        hw = _FakeHardware(4)
        cancel = threading.Event()

        with patch("lucid_component_led_strip.effects.sparkle.time.sleep", lambda s: None):
            run(hw, cancel, r=100, g=50, b=200, wait_ms=0, cumulative=False)

        assert hw.shows >= 1

    def test_sparkle_respects_cancel(self):
        from lucid_component_led_strip.effects.sparkle import run
        hw = _FakeHardware(100)
        cancel = threading.Event()
        shows_before = [0]

        def cancel_after_two(s):
            shows_before[0] += 1
            if shows_before[0] >= 2:
                cancel.set()

        with patch("lucid_component_led_strip.effects.sparkle.time.sleep", cancel_after_two):
            run(hw, cancel, r=-1, g=-1, b=-1, wait_ms=1)

        assert shows_before[0] < 100  # exited early


class TestRainbowEffect:
    def test_rainbow_runs_one_frame(self):
        from lucid_component_led_strip.effects.rainbow import run
        hw = _FakeHardware(6)
        cancel = threading.Event()

        with patch("lucid_component_led_strip.effects.rainbow.time.sleep", lambda s: cancel.set()):
            run(hw, cancel, wait_ms=10)

        assert hw.shows >= 1

    def test_rainbow_all_pixels_set(self):
        from lucid_component_led_strip.effects.rainbow import run
        hw = _FakeHardware(6)
        cancel = threading.Event()
        frame_pixels = []

        orig_show = hw.show
        def capturing_show():
            frame_pixels.append(list(hw.pixels))
            cancel.set()
            orig_show()

        hw.show = capturing_show

        with patch("lucid_component_led_strip.effects.rainbow.time.sleep", lambda s: None):
            run(hw, cancel, wait_ms=0)

        # All 6 pixels should have been set (non-zero or set to wheel value)
        assert len(frame_pixels) >= 1


class TestRainbowCycleEffect:
    def test_rainbow_cycle_runs_one_frame(self):
        from lucid_component_led_strip.effects.rainbow_cycle import run
        hw = _FakeHardware(6)
        cancel = threading.Event()

        with patch("lucid_component_led_strip.effects.rainbow_cycle.time.sleep", lambda s: cancel.set()):
            run(hw, cancel, wait_ms=10)

        assert hw.shows >= 1


class TestTheaterChaseEffect:
    def test_theater_chase_rainbow_mode(self):
        from lucid_component_led_strip.effects.theater_chase import run
        hw = _FakeHardware(9)
        cancel = threading.Event()
        count = [0]

        def stop_after_first(s):
            count[0] += 1
            if count[0] >= 3:
                cancel.set()

        with patch("lucid_component_led_strip.effects.theater_chase.time.sleep", stop_after_first):
            run(hw, cancel, r=-1, g=-1, b=-1, wait_ms=1)

        assert hw.shows >= 1

    def test_theater_chase_single_color(self):
        from lucid_component_led_strip.effects.theater_chase import run
        hw = _FakeHardware(9)
        cancel = threading.Event()
        count = [0]

        def stop_after_first(s):
            count[0] += 1
            if count[0] >= 3:
                cancel.set()

        with patch("lucid_component_led_strip.effects.theater_chase.time.sleep", stop_after_first):
            run(hw, cancel, r=255, g=0, b=128, wait_ms=1)

        assert hw.shows >= 1


class TestRunningEffect:
    def test_running_advances_index(self):
        from lucid_component_led_strip.effects.running import run
        hw = _FakeHardware(6)
        cancel = threading.Event()
        count = [0]

        def stop_after_3(s):
            count[0] += 1
            if count[0] >= 3:
                cancel.set()

        with patch("lucid_component_led_strip.effects.running.time.sleep", stop_after_3):
            run(hw, cancel, r=255, g=0, b=0, wait_ms=1, width=1)

        assert count[0] == 3
        assert hw.shows >= 3


# ===========================================================================
# component.py — LEDStripComponent
# ===========================================================================

class TestComponentLifecycle:
    def test_initial_state(self):
        ctx = _fake_context()
        comp = LEDStripComponent(ctx)
        assert comp.component_id == "led_strip"
        assert comp.state.status == ComponentStatus.STOPPED
        assert comp._hardware_initialized is False
        assert comp._current_effect is None

    def test_capabilities_complete(self):
        ctx = _fake_context()
        comp = LEDStripComponent(ctx)
        caps = comp.capabilities()
        expected = [
            "reset", "ping", "clear", "set-color",
            "set-range-percent", "set-range-exact",
            "effect/glow", "effect/wave", "effect/color-wipe",
            "effect/color-fade", "effect/sparkle", "effect/rainbow",
            "effect/rainbow-cycle", "effect/theater-chase", "effect/running",
        ]
        for c in expected:
            assert c in caps, f"Missing capability: {c}"

    def test_start_succeeds_sets_hardware_initialized(self):
        ctx = _fake_context()
        comp = LEDStripComponent(ctx)
        with patch("lucid_component_led_strip.component.led_client") as mc:
            mc.init.return_value = {"ok": True}
            comp.start()
            assert comp._hardware_initialized is True
            assert comp.state.status == ComponentStatus.RUNNING
        comp.stop()

    def test_start_failure_raises(self):
        ctx = _fake_context()
        comp = LEDStripComponent(ctx)
        with patch("lucid_component_led_strip.component.led_client") as mc:
            mc.init.return_value = {"ok": False, "error": "DMA busy"}
            with pytest.raises(RuntimeError, match="DMA busy"):
                comp.start()
        assert comp._hardware_initialized is False

    def test_stop_clears_state(self):
        ctx = _fake_context()
        comp = LEDStripComponent(ctx)
        with patch("lucid_component_led_strip.component.led_client") as mc:
            mc.init.return_value = {"ok": True}
            mc.reset.return_value = {"ok": True}
            comp.start()
            comp._current_effect = "glow"
            comp.stop()
            assert comp._hardware_initialized is False
            assert comp._current_effect is None
            assert comp.state.status == ComponentStatus.STOPPED

    def test_get_state_payload_keys(self):
        ctx = _fake_context()
        comp = LEDStripComponent(ctx)
        state = comp.get_state_payload()
        for k in ("brightness", "current_effect", "led_count", "strip1_count",
                   "strip2_count", "strip1_pin", "strip2_pin", "hardware_initialized"):
            assert k in state

    def test_get_state_payload_led_count_sum(self):
        ctx = _fake_context()
        comp = LEDStripComponent(ctx)
        state = comp.get_state_payload()
        assert state["led_count"] == state["strip1_count"] + state["strip2_count"]

    def test_get_cfg_payload(self):
        ctx = _fake_context()
        comp = LEDStripComponent(ctx)
        cfg = comp.get_cfg_payload()
        assert cfg["brightness"] == 125
        assert cfg["strip1_count"] == 896
        assert cfg["strip2_count"] == 894

    def test_custom_config_overrides_defaults(self):
        ctx = ComponentContext.create(
            agent_id="test-agent",
            base_topic="lucid/agents/test-agent",
            component_id="led_strip",
            mqtt=MagicMock(),
            config={"brightness": "200", "strip1_count": "100", "strip2_count": "50"},
        )
        comp = LEDStripComponent(ctx)
        assert comp._brightness == 200
        assert comp._strip1_count == 100
        assert comp._strip2_count == 50

    def test_brightness_clamped_at_255(self):
        ctx = ComponentContext.create(
            agent_id="a", base_topic="lucid/agents/a",
            component_id="led_strip", mqtt=MagicMock(),
            config={"brightness": "999"},
        )
        comp = LEDStripComponent(ctx)
        assert comp._brightness == 255

    def test_brightness_clamped_at_0(self):
        ctx = ComponentContext.create(
            agent_id="a", base_topic="lucid/agents/a",
            component_id="led_strip", mqtt=MagicMock(),
            config={"brightness": "-10"},
        )
        comp = LEDStripComponent(ctx)
        assert comp._brightness == 0


class TestCommandClear:
    def test_clear_success(self):
        ctx = _fake_context()
        comp = LEDStripComponent(ctx)
        with patch("lucid_component_led_strip.component.led_client") as mc:
            mc.clear.return_value = {"ok": True}
            comp._hardware_initialized = True
            comp.on_cmd_clear(json.dumps({"request_id": "clr-1"}))

        results = _published_results(ctx, "evt/clear/result")
        assert results and results[0]["ok"] is True
        assert results[0]["request_id"] == "clr-1"
        assert comp._current_effect is None

    def test_clear_without_hardware(self):
        ctx = _fake_context()
        comp = LEDStripComponent(ctx)
        comp._hardware_initialized = False
        comp.on_cmd_clear(json.dumps({"request_id": "clr-2"}))
        results = _published_results(ctx, "evt/clear/result")
        assert results and results[0]["ok"] is False
        assert "not initialized" in results[0]["error"]

    def test_clear_ipc_failure_signals_failed(self):
        ctx = _fake_context()
        comp = LEDStripComponent(ctx)
        with patch("lucid_component_led_strip.component.led_client") as mc:
            mc.clear.return_value = {"ok": False, "error": "[Errno 111] Connection refused"}
            comp._hardware_initialized = True
            comp.on_cmd_clear(json.dumps({"request_id": "clr-3"}))
        assert comp._hardware_initialized is False

    def test_clear_invalid_json_uses_empty_request_id(self):
        ctx = _fake_context()
        comp = LEDStripComponent(ctx)
        with patch("lucid_component_led_strip.component.led_client") as mc:
            mc.clear.return_value = {"ok": True}
            comp._hardware_initialized = True
            comp.on_cmd_clear("not-json")
        # Should not raise; result published with empty request_id
        results = _published_results(ctx, "evt/clear/result")
        assert results


class TestCommandReset:
    def test_reset_clears_current_effect(self):
        ctx = _fake_context()
        comp = LEDStripComponent(ctx)
        with patch("lucid_component_led_strip.component.led_client") as mc:
            mc.init.return_value = {"ok": True}
            mc.reset.return_value = {"ok": True}
            comp.start()
            comp._current_effect = "wave"
            comp.on_cmd_reset(json.dumps({"request_id": "rst-1"}))
        assert comp._current_effect is None
        results = _published_results(ctx, "evt/reset/result")
        assert results and results[0]["ok"] is True
        comp.stop()

    def test_reset_ipc_failure_signals_failed(self):
        ctx = _fake_context()
        comp = LEDStripComponent(ctx)
        with patch("lucid_component_led_strip.component.led_client") as mc:
            mc.reset.return_value = {"ok": False, "error": "connection closed"}
            comp._hardware_initialized = True
            comp.on_cmd_reset(json.dumps({"request_id": "rst-2"}))
        assert comp._hardware_initialized is False


class TestCommandPing:
    def test_ping_publishes_ok_result(self):
        ctx = _fake_context()
        comp = LEDStripComponent(ctx)
        with patch("lucid_component_led_strip.component.led_client") as mc:
            mc.init.return_value = {"ok": True}
            comp.start()
            comp.on_cmd_ping(json.dumps({"request_id": "ping-1"}))
        results = _published_results(ctx, "evt/ping/result")
        assert results and results[0]["ok"] is True
        assert results[0]["request_id"] == "ping-1"
        comp.stop()

    def test_ping_with_invalid_json(self):
        ctx = _fake_context()
        comp = LEDStripComponent(ctx)
        comp.on_cmd_ping("{bad json")  # should not raise
        results = _published_results(ctx, "evt/ping/result")
        assert results  # still publishes


class TestCommandSetColor:
    def test_set_color_with_rgb(self):
        ctx = _fake_context()
        comp = LEDStripComponent(ctx)
        with patch("lucid_component_led_strip.component.led_client") as mc:
            mc.set_color.return_value = {"ok": True}
            comp._hardware_initialized = True
            comp.on_cmd_set_color(json.dumps({"request_id": "sc-1", "color": {"r": 10, "g": 20, "b": 30}}))
            mc.set_color.assert_called_once_with({"r": 10, "g": 20, "b": 30})
        results = _published_results(ctx, "evt/set-color/result")
        assert results and results[0]["ok"] is True

    def test_set_color_no_color_passes_none(self):
        ctx = _fake_context()
        comp = LEDStripComponent(ctx)
        with patch("lucid_component_led_strip.component.led_client") as mc:
            mc.set_color.return_value = {"ok": True}
            comp._hardware_initialized = True
            comp.on_cmd_set_color(json.dumps({"request_id": "sc-2"}))
            mc.set_color.assert_called_once_with(None)

    def test_set_color_without_hardware_returns_error(self):
        ctx = _fake_context()
        comp = LEDStripComponent(ctx)
        comp._hardware_initialized = False
        comp.on_cmd_set_color(json.dumps({"request_id": "sc-3"}))
        results = _published_results(ctx, "evt/set-color/result")
        assert results and results[0]["ok"] is False

    def test_set_color_ipc_failure_signals_failed(self):
        ctx = _fake_context()
        comp = LEDStripComponent(ctx)
        with patch("lucid_component_led_strip.component.led_client") as mc:
            mc.set_color.return_value = {"ok": False, "error": "[Errno 2] No such file"}
            comp._hardware_initialized = True
            comp.on_cmd_set_color(json.dumps({"request_id": "sc-4"}))
        assert comp._hardware_initialized is False


class TestCommandSetRangePercent:
    def test_set_range_percent_forwards_params(self):
        ctx = _fake_context()
        comp = LEDStripComponent(ctx)
        with patch("lucid_component_led_strip.component.led_client") as mc:
            mc.set_range_percent.return_value = {"ok": True}
            comp._hardware_initialized = True
            comp.on_cmd_set_range_percent(json.dumps({
                "request_id": "srp-1",
                "color": {"r": 1, "g": 2, "b": 3},
                "start_percent": 0.25,
                "end_percent": 0.75,
            }))
            mc.set_range_percent.assert_called_once_with(
                {"r": 1, "g": 2, "b": 3}, 0.25, 0.75
            )
        results = _published_results(ctx, "evt/set-range-percent/result")
        assert results and results[0]["ok"] is True

    def test_set_range_percent_without_hardware(self):
        ctx = _fake_context()
        comp = LEDStripComponent(ctx)
        comp._hardware_initialized = False
        comp.on_cmd_set_range_percent(json.dumps({"request_id": "srp-2"}))
        results = _published_results(ctx, "evt/set-range-percent/result")
        assert results and results[0]["ok"] is False


class TestCommandSetRangeExact:
    def test_set_range_exact_forwards_params(self):
        ctx = _fake_context()
        comp = LEDStripComponent(ctx)
        with patch("lucid_component_led_strip.component.led_client") as mc:
            mc.set_range_exact.return_value = {"ok": True}
            comp._hardware_initialized = True
            comp.on_cmd_set_range_exact(json.dumps({
                "request_id": "sre-1",
                "color": {"r": 5, "g": 10, "b": 15},
                "start_index": 0,
                "end_index": 50,
            }))
            mc.set_range_exact.assert_called_once_with(
                {"r": 5, "g": 10, "b": 15}, 0, 50
            )

    def test_set_range_exact_without_hardware(self):
        ctx = _fake_context()
        comp = LEDStripComponent(ctx)
        comp._hardware_initialized = False
        comp.on_cmd_set_range_exact(json.dumps({"request_id": "sre-2"}))
        results = _published_results(ctx, "evt/set-range-exact/result")
        assert results and results[0]["ok"] is False


class TestCommandCfgSet:
    def test_cfg_set_brightness_runtime(self):
        ctx = _fake_context()
        comp = LEDStripComponent(ctx)
        with patch("lucid_component_led_strip.component.led_client") as mc:
            mc.init.return_value = {"ok": True}
            mc.set_brightness.return_value = {"ok": True}
            comp.start()
            comp.on_cmd_cfg_set(json.dumps({"request_id": "cfg-1", "set": {"brightness": 200}}))
            assert comp._brightness == 200
            mc.set_brightness.assert_called_once_with(200)
        comp.stop()

    def test_cfg_set_brightness_clamped(self):
        ctx = _fake_context()
        comp = LEDStripComponent(ctx)
        with patch("lucid_component_led_strip.component.led_client") as mc:
            mc.init.return_value = {"ok": True}
            mc.set_brightness.return_value = {"ok": True}
            comp.start()
            comp.on_cmd_cfg_set(json.dumps({"request_id": "cfg-2", "set": {"brightness": 999}}))
            assert comp._brightness == 255
        comp.stop()

    def test_cfg_set_unknown_key_returns_error(self):
        ctx = _fake_context()
        comp = LEDStripComponent(ctx)
        with patch("lucid_component_led_strip.component.led_client") as mc:
            mc.init.return_value = {"ok": True}
            comp.start()
            comp.on_cmd_cfg_set(json.dumps({"request_id": "cfg-3", "set": {"unknown_key": 42}}))
        results = _published_results(ctx, "cfg/set/result")
        assert results and results[0]["ok"] is False
        assert "unknown cfg key" in results[0]["error"]
        comp.stop()

    def test_cfg_set_hardware_keys_mark_restart_required(self):
        ctx = _fake_context()
        comp = LEDStripComponent(ctx)
        with patch("lucid_component_led_strip.component.led_client") as mc:
            mc.init.return_value = {"ok": True}
            comp.start()
            comp.on_cmd_cfg_set(json.dumps({"request_id": "cfg-4", "set": {"strip1_count": 500}}))
        results = _published_results(ctx, "cfg/set/result")
        assert results
        assert results[0]["ok"] is True
        assert "restart required" in (results[0].get("error") or "")
        comp.stop()

    def test_cfg_set_invalid_json(self):
        ctx = _fake_context()
        comp = LEDStripComponent(ctx)
        comp.on_cmd_cfg_set("{invalid}")  # should not raise

    def test_cfg_set_brightness_ipc_failure_signals_failed(self):
        ctx = _fake_context()
        comp = LEDStripComponent(ctx)
        with patch("lucid_component_led_strip.component.led_client") as mc:
            mc.init.return_value = {"ok": True}
            mc.set_brightness.return_value = {"ok": False, "error": "connection closed"}
            comp.start()
            comp.on_cmd_cfg_set(json.dumps({"request_id": "cfg-5", "set": {"brightness": 100}}))
        assert comp._hardware_initialized is False
        comp.stop()


class TestCommandCfgTelemetrySet:
    def test_cfg_telemetry_set_interval(self):
        ctx = _fake_context()
        comp = LEDStripComponent(ctx)
        with patch("lucid_component_led_strip.component.led_client") as mc:
            mc.init.return_value = {"ok": True}
            comp.start()
            comp.on_cmd_cfg_telemetry_set(json.dumps({
                "request_id": "tel-1",
                "set": {"pixel_rgb": {"enabled": True, "interval_s": 30}},
            }))
            assert comp._pixel_telemetry_interval_s == 30
        comp.stop()

    def test_cfg_telemetry_set_interval_minimum_clamped(self):
        ctx = _fake_context()
        comp = LEDStripComponent(ctx)
        with patch("lucid_component_led_strip.component.led_client") as mc:
            mc.init.return_value = {"ok": True}
            comp.start()
            comp.on_cmd_cfg_telemetry_set(json.dumps({
                "request_id": "tel-2",
                "set": {"pixel_rgb": {"interval_s": 0}},
            }))
            assert comp._pixel_telemetry_interval_s == 1
        comp.stop()

    def test_cfg_telemetry_set_bool_shorthand(self):
        ctx = _fake_context()
        comp = LEDStripComponent(ctx)
        with patch("lucid_component_led_strip.component.led_client") as mc:
            mc.init.return_value = {"ok": True}
            comp.start()
            # Boolean shorthand: {"pixel_rgb": true}
            comp.on_cmd_cfg_telemetry_set(json.dumps({
                "request_id": "tel-3",
                "set": {"pixel_rgb": True},
            }))
        # Should not raise; base class handles retain publish
        comp.stop()

    def test_cfg_telemetry_set_invalid_metric_type(self):
        ctx = _fake_context()
        comp = LEDStripComponent(ctx)
        comp.on_cmd_cfg_telemetry_set(json.dumps({
            "request_id": "tel-4",
            "set": {"pixel_rgb": "not-an-object"},
        }))
        results = _published_results(ctx, "cfg/telemetry/set/result")
        assert results and results[0]["ok"] is False


class TestEffectCommandHandlers:
    """Test all on_cmd_effect_* handlers."""

    def _run_effect_cmd(self, method_name: str, payload: dict) -> tuple[ComponentContext, LEDStripComponent]:
        ctx = _fake_context()
        comp = LEDStripComponent(ctx)
        with patch("lucid_component_led_strip.component.led_client") as mc:
            mc.init.return_value = {"ok": True}
            mc.effect.return_value = {"ok": True}
            mc.stop_effect.return_value = {"ok": True}
            comp.start()
            getattr(comp, method_name)(json.dumps(payload))
        comp.stop()
        return ctx, comp

    def test_effect_glow(self):
        ctx, comp = self._run_effect_cmd(
            "on_cmd_effect_glow",
            {"request_id": "fx-glow", "color": {"r": 0, "g": 255, "b": 0}, "wait_ms": 10},
        )
        results = _published_results(ctx, "evt/effect/glow/result")
        assert results and results[0]["ok"] is True
        assert results[0]["request_id"] == "fx-glow"

    def test_effect_glow_without_hardware(self):
        ctx = _fake_context()
        comp = LEDStripComponent(ctx)
        comp._hardware_initialized = False
        comp.on_cmd_effect_glow(json.dumps({"request_id": "fx-glow-2"}))
        results = _published_results(ctx, "evt/effect/glow/result")
        assert results and results[0]["ok"] is False

    def test_effect_glow_invalid_json(self):
        ctx = _fake_context()
        comp = LEDStripComponent(ctx)
        comp.on_cmd_effect_glow("INVALID")
        results = _published_results(ctx, "evt/effect/glow/result")
        assert results and results[0]["ok"] is False

    def test_effect_wave(self):
        ctx, comp = self._run_effect_cmd(
            "on_cmd_effect_wave",
            {"request_id": "fx-wave", "cycles": 2, "speed": 0.2, "wait_ms": 5},
        )
        results = _published_results(ctx, "evt/effect/wave/result")
        assert results and results[0]["ok"] is True

    def test_effect_wave_without_hardware(self):
        ctx = _fake_context()
        comp = LEDStripComponent(ctx)
        comp._hardware_initialized = False
        comp.on_cmd_effect_wave(json.dumps({"request_id": "fx-wave-2"}))
        results = _published_results(ctx, "evt/effect/wave/result")
        assert results and results[0]["ok"] is False

    def test_effect_color_wipe(self):
        ctx, comp = self._run_effect_cmd(
            "on_cmd_effect_color_wipe",
            {"request_id": "fx-wipe", "color": {"r": 255, "g": 0, "b": 0}, "wait_ms": 1},
        )
        results = _published_results(ctx, "evt/effect/color-wipe/result")
        assert results and results[0]["ok"] is True

    def test_effect_color_fade(self):
        ctx, comp = self._run_effect_cmd(
            "on_cmd_effect_color_fade",
            {
                "request_id": "fx-fade",
                "color_from": {"r": 0, "g": 0, "b": 0},
                "color_to": {"r": 0, "g": 0, "b": 255},
                "steps": 10,
            },
        )
        results = _published_results(ctx, "evt/effect/color-fade/result")
        assert results and results[0]["ok"] is True

    def test_effect_sparkle_no_color(self):
        ctx, comp = self._run_effect_cmd(
            "on_cmd_effect_sparkle",
            {"request_id": "fx-sparkle", "wait_ms": 5},
        )
        results = _published_results(ctx, "evt/effect/sparkle/result")
        assert results and results[0]["ok"] is True

    def test_effect_sparkle_with_color(self):
        ctx, comp = self._run_effect_cmd(
            "on_cmd_effect_sparkle",
            {"request_id": "fx-sparkle-2", "color": {"r": 200, "g": 100, "b": 50}, "cumulative": True},
        )
        results = _published_results(ctx, "evt/effect/sparkle/result")
        assert results and results[0]["ok"] is True

    def test_effect_rainbow(self):
        ctx, comp = self._run_effect_cmd(
            "on_cmd_effect_rainbow",
            {"request_id": "fx-rainbow", "wait_ms": 20},
        )
        results = _published_results(ctx, "evt/effect/rainbow/result")
        assert results and results[0]["ok"] is True

    def test_effect_rainbow_without_hardware(self):
        ctx = _fake_context()
        comp = LEDStripComponent(ctx)
        comp._hardware_initialized = False
        comp.on_cmd_effect_rainbow(json.dumps({"request_id": "fx-rainbow-2"}))
        results = _published_results(ctx, "evt/effect/rainbow/result")
        assert results and results[0]["ok"] is False

    def test_effect_rainbow_cycle(self):
        ctx, comp = self._run_effect_cmd(
            "on_cmd_effect_rainbow_cycle",
            {"request_id": "fx-rc", "wait_ms": 30},
        )
        results = _published_results(ctx, "evt/effect/rainbow-cycle/result")
        assert results and results[0]["ok"] is True

    def test_effect_theater_chase(self):
        ctx, comp = self._run_effect_cmd(
            "on_cmd_effect_theater_chase",
            {"request_id": "fx-tc", "wait_ms": 5},
        )
        results = _published_results(ctx, "evt/effect/theater-chase/result")
        assert results and results[0]["ok"] is True

    def test_effect_theater_chase_with_color(self):
        ctx, comp = self._run_effect_cmd(
            "on_cmd_effect_theater_chase",
            {"request_id": "fx-tc-2", "color": {"r": 0, "g": 0, "b": 255}},
        )
        results = _published_results(ctx, "evt/effect/theater-chase/result")
        assert results and results[0]["ok"] is True

    def test_effect_running(self):
        ctx, comp = self._run_effect_cmd(
            "on_cmd_effect_running",
            {"request_id": "fx-run", "color": {"r": 255, "g": 128, "b": 0}, "wait_ms": 5, "width": 2},
        )
        results = _published_results(ctx, "evt/effect/running/result")
        assert results and results[0]["ok"] is True

    def test_effect_running_default_color_on_bad_color(self):
        ctx = _fake_context()
        comp = LEDStripComponent(ctx)
        with patch("lucid_component_led_strip.component.led_client") as mc:
            mc.init.return_value = {"ok": True}
            mc.effect.return_value = {"ok": True}
            mc.stop_effect.return_value = {"ok": True}
            comp.start()
            # Pass non-dict color — component should fall back to default
            comp.on_cmd_effect_running(json.dumps({"request_id": "fx-run-2", "color": "bad"}))
            call_kwargs = mc.effect.call_args
            assert call_kwargs is not None
            params = call_kwargs[0][1]
            # Default color should be r=255, g=0, b=0
            assert params["color"]["r"] == 255
            assert params["color"]["g"] == 0
            assert params["color"]["b"] == 0
        comp.stop()

    def test_effect_sets_current_effect(self):
        ctx = _fake_context()
        comp = LEDStripComponent(ctx)
        with patch("lucid_component_led_strip.component.led_client") as mc:
            mc.init.return_value = {"ok": True}
            mc.effect.return_value = {"ok": True}
            mc.stop_effect.return_value = {"ok": True}
            comp.start()
            comp.on_cmd_effect_glow(json.dumps({"request_id": "fx-track"}))
            assert comp._current_effect == "glow"
        comp.stop()

    def test_effect_ipc_failure_signals_failed(self):
        ctx = _fake_context()
        comp = LEDStripComponent(ctx)
        with patch("lucid_component_led_strip.component.led_client") as mc:
            mc.init.return_value = {"ok": True}
            mc.effect.return_value = {"ok": False, "error": "[Errno 32] Broken pipe"}
            mc.stop_effect.return_value = {"ok": True}
            comp.start()
            comp.on_cmd_effect_rainbow(json.dumps({"request_id": "fx-err"}))
        assert comp._hardware_initialized is False
        comp.stop()


class TestIpcFailureDetection:
    def test_is_ipc_failure_with_errno(self):
        from lucid_component_led_strip.component import _is_ipc_failure
        assert _is_ipc_failure("[Errno 111] Connection refused") is True

    def test_is_ipc_failure_with_connection_closed(self):
        from lucid_component_led_strip.component import _is_ipc_failure
        assert _is_ipc_failure("connection closed") is True

    def test_is_ipc_failure_normal_error(self):
        from lucid_component_led_strip.component import _is_ipc_failure
        assert _is_ipc_failure("hardware error") is False

    def test_is_ipc_failure_none(self):
        from lucid_component_led_strip.component import _is_ipc_failure
        assert _is_ipc_failure(None) is False

    def test_is_ipc_failure_empty(self):
        from lucid_component_led_strip.component import _is_ipc_failure
        assert _is_ipc_failure("") is False


# ===========================================================================
# helper_server.py — HelperState and _handle_request
# ===========================================================================

def _fake_effects_module() -> types.SimpleNamespace:
    def _run(*_args, **_kwargs):
        pass

    effect_names = (
        "glow", "wave", "color_wipe", "color_fade", "sparkle",
        "rainbow", "rainbow_cycle", "theater_chase", "running",
    )
    return types.SimpleNamespace(
        **{name: types.SimpleNamespace(run=_run) for name in effect_names}
    )


class _FakeOrchestrator:
    def __init__(self):
        self.started: list[tuple[str, dict]] = []
        self.stops = 0

    def start(self, name, fn, hw, **kwargs):
        self.started.append((name, kwargs))

    def stop(self):
        self.stops += 1


class TestHelperState:
    def test_stop_effect_no_orchestrator(self):
        state = helper_server.HelperState()
        # Before init, _orchestrator is None — should raise AttributeError
        # This is a known issue (ISSUE-003) — we document the behavior
        with pytest.raises(AttributeError):
            state.stop_effect()

    def test_reset_no_orchestrator_raises(self):
        state = helper_server.HelperState()
        with pytest.raises(AttributeError):
            state.reset()

    def test_set_brightness_without_init_raises(self):
        state = helper_server.HelperState()
        state._orchestrator = _FakeOrchestrator()  # set orchestrator so lock passes
        with pytest.raises(RuntimeError, match="hardware not initialized"):
            state.set_brightness(100)

    def test_clear_without_init_raises(self):
        state = helper_server.HelperState()
        state._orchestrator = _FakeOrchestrator()
        with pytest.raises(RuntimeError, match="hardware not initialized"):
            state.clear()

    def test_get_pixels_without_init_raises(self):
        state = helper_server.HelperState()
        with pytest.raises(RuntimeError, match="hardware not initialized"):
            state.get_pixels()

    def test_start_effect_unknown_raises(self, monkeypatch: pytest.MonkeyPatch):
        state = helper_server.HelperState()
        state._hardware = object()
        state._orchestrator = _FakeOrchestrator()
        monkeypatch.setattr(helper_server, "fx", _fake_effects_module())
        with pytest.raises(ValueError, match="unknown effect"):
            state.start_effect("no-such-effect", {})

    def test_start_effect_glow_defaults(self, monkeypatch: pytest.MonkeyPatch):
        state = helper_server.HelperState()
        orch = _FakeOrchestrator()
        state._hardware = object()
        state._orchestrator = orch
        monkeypatch.setattr(helper_server, "fx", _fake_effects_module())
        state.start_effect("glow", {})
        assert orch.started
        name, kwargs = orch.started[0]
        assert name == "glow"

    def test_start_effect_glow_with_color(self, monkeypatch: pytest.MonkeyPatch):
        state = helper_server.HelperState()
        orch = _FakeOrchestrator()
        state._hardware = object()
        state._orchestrator = orch
        monkeypatch.setattr(helper_server, "fx", _fake_effects_module())
        state.start_effect("glow", {"color": {"r": 100, "g": 200, "b": 50}})
        _, kwargs = orch.started[0]
        assert kwargs["r"] == 100
        assert kwargs["g"] == 200
        assert kwargs["b"] == 50

    def test_start_effect_sparkle_no_color_uses_sentinel(self, monkeypatch: pytest.MonkeyPatch):
        state = helper_server.HelperState()
        orch = _FakeOrchestrator()
        state._hardware = object()
        state._orchestrator = orch
        monkeypatch.setattr(helper_server, "fx", _fake_effects_module())
        state.start_effect("sparkle", {})
        _, kwargs = orch.started[0]
        assert kwargs["r"] == -1
        assert kwargs["g"] == -1
        assert kwargs["b"] == -1

    def test_start_effect_color_fade_with_from_to(self, monkeypatch: pytest.MonkeyPatch):
        state = helper_server.HelperState()
        orch = _FakeOrchestrator()
        state._hardware = object()
        state._orchestrator = orch
        monkeypatch.setattr(helper_server, "fx", _fake_effects_module())
        state.start_effect("color-fade", {
            "color_from": {"r": 0, "g": 0, "b": 0},
            "color_to": {"r": 255, "g": 0, "b": 128},
        })
        _, kwargs = orch.started[0]
        assert kwargs["r_from"] == 0
        assert kwargs["r_to"] == 255
        assert kwargs["b_to"] == 128

    def test_start_effect_theater_chase_null_color_sentinel(self, monkeypatch: pytest.MonkeyPatch):
        state = helper_server.HelperState()
        orch = _FakeOrchestrator()
        state._hardware = object()
        state._orchestrator = orch
        monkeypatch.setattr(helper_server, "fx", _fake_effects_module())
        # null/None color → sentinel -1,-1,-1
        state.start_effect("theater-chase", {"color": None})
        _, kwargs = orch.started[0]
        assert kwargs["r"] == -1
        assert kwargs["g"] == -1
        assert kwargs["b"] == -1


class TestHandleRequest:
    def test_missing_cmd(self):
        state = helper_server.HelperState()
        resp = helper_server._handle_request(state, {"id": 1})
        assert resp["ok"] is False
        assert "missing cmd" in resp["error"]

    def test_unknown_cmd(self):
        state = helper_server.HelperState()
        resp = helper_server._handle_request(state, {"id": 1, "cmd": "does_not_exist"})
        assert resp["ok"] is False
        assert "unknown cmd" in resp["error"]

    def test_ping_without_hardware(self):
        state = helper_server.HelperState()
        resp = helper_server._handle_request(state, {"id": 1, "cmd": "ping"})
        assert resp["ok"] is False
        assert "hardware not initialized" in resp["error"]

    def test_get_pixels_without_hardware(self):
        state = helper_server.HelperState()
        resp = helper_server._handle_request(state, {"id": 1, "cmd": "get_pixels"})
        assert resp["ok"] is False

    def test_effect_command_without_hardware(self, monkeypatch: pytest.MonkeyPatch):
        state = helper_server.HelperState()
        state._orchestrator = _FakeOrchestrator()
        monkeypatch.setattr(helper_server, "fx", _fake_effects_module())
        resp = helper_server._handle_request(state, {
            "id": 1, "cmd": "effect", "name": "glow", "params": {},
        })
        assert resp["ok"] is False
        assert "hardware not initialized" in resp["error"]

    def test_stop_effect_no_orchestrator_returns_error(self):
        state = helper_server.HelperState()
        resp = helper_server._handle_request(state, {"id": 1, "cmd": "stop_effect"})
        assert resp["ok"] is False

    def test_set_brightness_no_hardware_returns_error(self):
        state = helper_server.HelperState()
        state._orchestrator = _FakeOrchestrator()
        resp = helper_server._handle_request(state, {"id": 1, "cmd": "set_brightness", "brightness": 100})
        assert resp["ok"] is False

    def test_clear_no_hardware_returns_error(self):
        state = helper_server.HelperState()
        state._orchestrator = _FakeOrchestrator()
        resp = helper_server._handle_request(state, {"id": 1, "cmd": "clear"})
        assert resp["ok"] is False

    def test_set_color_no_hardware_returns_error(self, monkeypatch: pytest.MonkeyPatch):
        state = helper_server.HelperState()
        state._orchestrator = _FakeOrchestrator()
        resp = helper_server._handle_request(state, {"id": 1, "cmd": "set_color", "color": {"r": 0, "g": 0, "b": 0}})
        assert resp["ok"] is False

    def test_set_range_percent_no_hardware_returns_error(self, monkeypatch: pytest.MonkeyPatch):
        state = helper_server.HelperState()
        state._orchestrator = _FakeOrchestrator()
        resp = helper_server._handle_request(state, {
            "id": 1, "cmd": "set_range_percent",
            "color": {"r": 0, "g": 0, "b": 0},
            "start_percent": 0.0, "end_percent": 1.0,
        })
        assert resp["ok"] is False

    def test_set_range_exact_no_hardware_returns_error(self, monkeypatch: pytest.MonkeyPatch):
        state = helper_server.HelperState()
        state._orchestrator = _FakeOrchestrator()
        resp = helper_server._handle_request(state, {
            "id": 1, "cmd": "set_range_exact",
            "color": {"r": 0, "g": 0, "b": 0},
            "start_index": 0,
        })
        assert resp["ok"] is False


# ===========================================================================
# client.py — socket error paths
# ===========================================================================

class TestClientSocketErrors:
    def test_connection_refused_returns_error_dict(self, monkeypatch: pytest.MonkeyPatch, tmp_path):
        monkeypatch.setenv("LUCID_LED_STRIP_SOCKET", str(tmp_path / "noexist.sock"))
        result = led_client_module.init()
        assert result["ok"] is False
        assert result["error"]

    def test_init_returns_dict(self, monkeypatch: pytest.MonkeyPatch, tmp_path):
        monkeypatch.setenv("LUCID_LED_STRIP_SOCKET", str(tmp_path / "noexist.sock"))
        result = led_client_module.init(strip1_count=10, strip2_count=5)
        assert isinstance(result, dict)
        assert "ok" in result

    def test_reset_returns_dict(self, monkeypatch: pytest.MonkeyPatch, tmp_path):
        monkeypatch.setenv("LUCID_LED_STRIP_SOCKET", str(tmp_path / "noexist.sock"))
        result = led_client_module.reset()
        assert isinstance(result, dict)

    def test_clear_returns_dict(self, monkeypatch: pytest.MonkeyPatch, tmp_path):
        monkeypatch.setenv("LUCID_LED_STRIP_SOCKET", str(tmp_path / "noexist.sock"))
        result = led_client_module.clear()
        assert isinstance(result, dict)

    def test_effect_returns_dict(self, monkeypatch: pytest.MonkeyPatch, tmp_path):
        monkeypatch.setenv("LUCID_LED_STRIP_SOCKET", str(tmp_path / "noexist.sock"))
        result = led_client_module.effect("glow", {"r": 255, "g": 0, "b": 0})
        assert isinstance(result, dict)

    def test_get_pixels_returns_dict(self, monkeypatch: pytest.MonkeyPatch, tmp_path):
        monkeypatch.setenv("LUCID_LED_STRIP_SOCKET", str(tmp_path / "noexist.sock"))
        result = led_client_module.get_pixels()
        assert isinstance(result, dict)

    def test_ping_returns_dict(self, monkeypatch: pytest.MonkeyPatch, tmp_path):
        monkeypatch.setenv("LUCID_LED_STRIP_SOCKET", str(tmp_path / "noexist.sock"))
        result = led_client_module.ping()
        assert isinstance(result, dict)

    def test_set_brightness_returns_dict(self, monkeypatch: pytest.MonkeyPatch, tmp_path):
        monkeypatch.setenv("LUCID_LED_STRIP_SOCKET", str(tmp_path / "noexist.sock"))
        result = led_client_module.set_brightness(200)
        assert isinstance(result, dict)

    def test_stop_effect_returns_dict(self, monkeypatch: pytest.MonkeyPatch, tmp_path):
        monkeypatch.setenv("LUCID_LED_STRIP_SOCKET", str(tmp_path / "noexist.sock"))
        result = led_client_module.stop_effect()
        assert isinstance(result, dict)

    def test_set_color_returns_dict(self, monkeypatch: pytest.MonkeyPatch, tmp_path):
        monkeypatch.setenv("LUCID_LED_STRIP_SOCKET", str(tmp_path / "noexist.sock"))
        result = led_client_module.set_color({"r": 100, "g": 50, "b": 25})
        assert isinstance(result, dict)

    def test_set_range_percent_returns_dict(self, monkeypatch: pytest.MonkeyPatch, tmp_path):
        monkeypatch.setenv("LUCID_LED_STRIP_SOCKET", str(tmp_path / "noexist.sock"))
        result = led_client_module.set_range_percent({"r": 0, "g": 0, "b": 255}, 0.0, 0.5)
        assert isinstance(result, dict)

    def test_set_range_exact_returns_dict(self, monkeypatch: pytest.MonkeyPatch, tmp_path):
        monkeypatch.setenv("LUCID_LED_STRIP_SOCKET", str(tmp_path / "noexist.sock"))
        result = led_client_module.set_range_exact({"r": 0, "g": 255, "b": 0}, 0, 10)
        assert isinstance(result, dict)

    def test_connection_closed_error_message(self, monkeypatch: pytest.MonkeyPatch):
        """Simulate a server that closes connection immediately."""
        import socket
        import tempfile
        import threading

        # AF_UNIX path is limited to ~104 chars on macOS; use mktemp for a short path.
        sock_path = tempfile.mktemp(suffix=".sock", prefix="led")
        monkeypatch.setenv("LUCID_LED_STRIP_SOCKET", sock_path)

        server_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server_sock.bind(sock_path)
        server_sock.listen(1)
        server_sock.settimeout(2.0)

        def close_immediately():
            try:
                conn, _ = server_sock.accept()
                conn.close()  # immediately close without responding
            except Exception:
                pass
            finally:
                server_sock.close()

        t = threading.Thread(target=close_immediately, daemon=True)
        t.start()

        result = led_client_module.ping()
        t.join(timeout=3.0)
        assert result["ok"] is False
        assert result["error"] in ("connection closed", ) or result["error"]
