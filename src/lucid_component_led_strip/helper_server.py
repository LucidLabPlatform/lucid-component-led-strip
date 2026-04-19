"""
LED strip helper daemon — runs as root, owns rpi_ws281x hardware.

Listens on a Unix socket for JSON-line commands from the LUCID component
(running as normal user). Handles init, reset, effects, and low-level strip control.

Start: lucid-led-strip-helper (or python -m lucid_component_led_strip.helper_server)
Socket: LUCID_LED_STRIP_SOCKET or /run/lucid/led-strip.sock
"""
from __future__ import annotations

import json
import logging
import os
import socket
import threading

# Lazy-loaded so the package can be installed without [pi]; set in run_server()
fx = None
EffectOrchestrator = None
LEDStripHardware = None

from .protocol import (  # noqa: E402
    CMD_CLEAR,
    CMD_EFFECT,
    CMD_GET_PIXELS,
    CMD_INIT,
    CMD_PING,
    CMD_RESET,
    CMD_SET_BRIGHTNESS,
    CMD_SET_COLOR,
    CMD_SET_RANGE_EXACT,
    CMD_SET_RANGE_PERCENT,
    CMD_STOP_EFFECT,
    DEFAULT_SOCKET_PATH,
)

logger = logging.getLogger(__name__)

# Group name for socket access (lucid user must be in this group, or socket chmod 0666)
LUCID_GROUP = "lucid"
SOCKET_MODE = 0o660


def _get_socket_path() -> str:
    return os.environ.get("LUCID_LED_STRIP_SOCKET", DEFAULT_SOCKET_PATH)


def _gid_for(name: str) -> int | None:
    try:
        import grp
        return grp.getgrnam(name).gr_gid
    except (ImportError, KeyError):
        return None


class HelperState:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._hardware = None
        self._orchestrator = None
        self._config: dict | None = None

    def init(self, strip1_count: int, strip2_count: int, strip1_pin: int, strip2_pin: int, brightness: int) -> None:
        if self._orchestrator is None:
            self._orchestrator = EffectOrchestrator()
        with self._lock:
            self._orchestrator.stop()
            if self._hardware is not None:
                try:
                    self._hardware.clear_all()
                except Exception:
                    logger.warning("Failed to clear LEDs before hardware reinit; continuing", exc_info=True)
            self._hardware = LEDStripHardware(
                strip1_count=strip1_count,
                strip2_count=strip2_count,
                strip1_pin=strip1_pin,
                strip2_pin=strip2_pin,
                brightness=brightness,
            )
            self._config = {
                "strip1_count": strip1_count,
                "strip2_count": strip2_count,
                "strip1_pin": strip1_pin,
                "strip2_pin": strip2_pin,
                "brightness": brightness,
            }
        logger.info("Hardware initialized: %d + %d LEDs", strip1_count, strip2_count)

    def _hw(self) -> LEDStripHardware | None:
        with self._lock:
            return self._hardware

    def reset(self) -> None:
        with self._lock:
            self._orchestrator.stop()
            if self._hardware is not None:
                self._hardware.clear_all()

    def stop_effect(self) -> None:
        with self._lock:
            self._orchestrator.stop()

    def set_brightness(self, value: int) -> None:
        with self._lock:
            if self._hardware is None:
                raise RuntimeError("hardware not initialized")
            self._hardware.set_brightness(value)
            if self._config is not None:
                self._config["brightness"] = value

    def clear(self) -> None:
        with self._lock:
            if self._hardware is None:
                raise RuntimeError("hardware not initialized")
            self._orchestrator.stop()
            self._hardware.clear_all()

    def get_pixels(self) -> list[list[int]]:
        """Return current pixel buffer as list of [r,g,b] per pixel."""
        with self._lock:
            if self._hardware is None:
                raise RuntimeError("hardware not initialized")
            return self._hardware.get_pixel_buffer()

    def set_color(self, color: dict | None) -> None:
        from rpi_ws281x import Color
        with self._lock:
            if self._hardware is None:
                raise RuntimeError("hardware not initialized")
            self._orchestrator.stop()
            if color:
                self._hardware.set_color_all(Color(int(color["r"]), int(color["g"]), int(color["b"])))
            else:
                self._hardware.set_white_all()

    def set_range_percent(self, color: dict, start_percent: float, end_percent: float) -> None:
        from rpi_ws281x import Color
        with self._lock:
            if self._hardware is None:
                raise RuntimeError("hardware not initialized")
            self._orchestrator.stop()
            self._hardware.set_color_range_percent(
                Color(int(color["r"]), int(color["g"]), int(color["b"])),
                start_percent,
                end_percent,
            )

    def set_range_exact(self, color: dict, start_index: int, end_index: int) -> None:
        from rpi_ws281x import Color
        with self._lock:
            if self._hardware is None:
                raise RuntimeError("hardware not initialized")
            self._orchestrator.stop()
            self._hardware.set_color_range_exact(
                Color(int(color["r"]), int(color["g"]), int(color["b"])),
                start_index,
                end_index,
            )

    def start_effect(self, name: str, params: dict) -> None:
        with self._lock:
            hw = self._hardware
        if hw is None:
            raise RuntimeError("hardware not initialized")
        effect_map = {
            "glow": (fx.glow.run, {"r": 255, "g": 255, "b": 255, "wait_ms": 10}),
            "wave": (fx.wave.run, {"r": 255, "g": 255, "b": 255, "cycles": 1, "speed": 0.1, "wait_ms": 10}),
            "color-wipe": (fx.color_wipe.run, {"r": 255, "g": 255, "b": 255, "wait_ms": 50}),
            "color-fade": (fx.color_fade.run, {"wait_ms": 20, "steps": 100}),
            "sparkle": (fx.sparkle.run, {"r": -1, "g": -1, "b": -1, "wait_ms": 50, "cumulative": False}),
            "rainbow": (fx.rainbow.run, {"wait_ms": 50}),
            "rainbow-cycle": (fx.rainbow_cycle.run, {"wait_ms": 50}),
            "theater-chase": (fx.theater_chase.run, {"r": 255, "g": 255, "b": 255, "wait_ms": 50, "spacing": 3, "width": 1}),
            "running": (fx.running.run, {"r": 255, "g": 255, "b": 255, "wait_ms": 10, "width": 1}),
        }
        if name not in effect_map:
            raise ValueError(f"unknown effect: {name}")
        fn, defaults = effect_map[name]
        # color=None is intentional (means "use rainbow/multicolor sentinel") so
        # preserve it; filter None from all other optional params.
        kwargs = {**defaults, **{k: v for k, v in params.items() if k in ("color", "color_from", "color_to") or v is not None}}
        # Normalize color params (r,g,b) and optional multicolor sentinel
        if "color" in kwargs:
            if isinstance(kwargs["color"], dict):
                c = kwargs["color"]
                kwargs["r"], kwargs["g"], kwargs["b"] = int(c.get("r", 255)), int(c.get("g", 255)), int(c.get("b", 255))
            else:
                kwargs["r"], kwargs["g"], kwargs["b"] = -1, -1, -1
            del kwargs["color"]
        if "color_from" in kwargs and isinstance(kwargs["color_from"], dict):
            cf = kwargs["color_from"]
            kwargs["r_from"] = int(cf["r"])
            kwargs["g_from"] = int(cf["g"])
            kwargs["b_from"] = int(cf["b"])
            del kwargs["color_from"]
        if "color_to" in kwargs and isinstance(kwargs["color_to"], dict):
            ct = kwargs["color_to"]
            kwargs["r_to"] = int(ct["r"])
            kwargs["g_to"] = int(ct["g"])
            kwargs["b_to"] = int(ct["b"])
            del kwargs["color_to"]
        with self._lock:
            self._orchestrator.start(name, fn, hw, **kwargs)


def _handle_request(state: HelperState, req: dict) -> dict:
    rid = req.get("id")
    cmd = req.get("cmd")
    if not cmd:
        return {"id": rid, "ok": False, "error": "missing cmd"}
    try:
        if cmd == CMD_PING:
            # No-op to check connectivity / hardware ready
            if state._hw() is None:
                return {"id": rid, "ok": False, "error": "hardware not initialized"}
            return {"id": rid, "ok": True}
        if cmd == CMD_INIT:
            state.init(
                strip1_count=int(req.get("strip1_count", 896)),
                strip2_count=int(req.get("strip2_count", 894)),
                strip1_pin=int(req.get("strip1_pin", 18)),
                strip2_pin=int(req.get("strip2_pin", 13)),
                brightness=max(0, min(255, int(req.get("brightness", 125)))),
            )
        elif cmd == CMD_RESET:
            state.reset()
        elif cmd == CMD_STOP_EFFECT:
            state.stop_effect()
        elif cmd == CMD_SET_BRIGHTNESS:
            state.set_brightness(max(0, min(255, int(req.get("brightness", 125)))))
        elif cmd == CMD_CLEAR:
            state.clear()
        elif cmd == CMD_SET_COLOR:
            state.set_color(req.get("color"))
        elif cmd == CMD_SET_RANGE_PERCENT:
            state.set_range_percent(
                req.get("color", {"r": 255, "g": 255, "b": 255}),
                float(req.get("start_percent", 0.0)),
                float(req.get("end_percent", 1.0)),
            )
        elif cmd == CMD_SET_RANGE_EXACT:
            hw = state._hw()
            end_index = req.get("end_index")
            if end_index is None and hw is not None:
                end_index = hw.LED_COUNT
            else:
                end_index = int(end_index) if end_index is not None else 0
            state.set_range_exact(
                req.get("color", {"r": 255, "g": 255, "b": 255}),
                int(req.get("start_index", 0)),
                end_index,
            )
        elif cmd == CMD_EFFECT:
            state.start_effect(
                str(req.get("name", "")),
                req.get("params") or {},
            )
        elif cmd == CMD_GET_PIXELS:
            pixels = state.get_pixels()
            return {"id": rid, "ok": True, "pixels": pixels}
        else:
            return {"id": rid, "ok": False, "error": f"unknown cmd: {cmd}"}
        return {"id": rid, "ok": True}
    except Exception as e:
        logger.exception("Command %s failed", cmd)
        return {"id": rid, "ok": False, "error": str(e)}


def _serve_connection(state: HelperState, conn: socket.socket) -> None:
    buf = b""
    try:
        while True:
            data = conn.recv(4096)
            if not data:
                break
            buf += data
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line = line.strip()
                if not line:
                    continue
                try:
                    req = json.loads(line.decode("utf-8"))
                except (json.JSONDecodeError, UnicodeDecodeError) as e:
                    resp = {"id": None, "ok": False, "error": str(e)}
                else:
                    resp = _handle_request(state, req)
                conn.sendall((json.dumps(resp) + "\n").encode("utf-8"))
    except (ConnectionResetError, BrokenPipeError, OSError):
        pass
    finally:
        try:
            conn.close()
        except OSError:
            pass


def run_server() -> None:
    global LEDStripHardware, EffectOrchestrator, fx
    try:
        from . import effects as _fx
        from .effects import EffectOrchestrator as _OC
        from .hardware import LEDStripHardware as _HW
        LEDStripHardware = _HW
        EffectOrchestrator = _OC
        fx = _fx
    except ImportError as e:
        logger.error("rpi_ws281x not available (on Pi: pip install .[pi]): %s", e)
        raise SystemExit(1) from e

    path = _get_socket_path()
    sock_dir = os.path.dirname(path)
    if sock_dir:
        os.makedirs(sock_dir, mode=0o755, exist_ok=True)
        # Allow group lucid to access the directory so they can connect to the socket
        try:
            gid = _gid_for(LUCID_GROUP)
            if gid is not None:
                os.chown(sock_dir, 0, gid)
                os.chmod(sock_dir, 0o775)
        except OSError:
            pass

    if os.path.exists(path):
        os.unlink(path)

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(path)
    try:
        gid = _gid_for(LUCID_GROUP)
        if gid is not None:
            os.chown(path, 0, gid)
        os.chmod(path, SOCKET_MODE)
    except OSError:
        pass
    server.listen(4)
    logger.info("Listening on %s", path)

    state = HelperState()
    while True:
        try:
            conn, _ = server.accept()
        except OSError:
            break
        t = threading.Thread(target=_serve_connection, args=(state, conn), daemon=True)
        t.start()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    run_server()


if __name__ == "__main__":
    main()
