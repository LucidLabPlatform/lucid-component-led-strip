"""
IPC client for the LED strip helper daemon.

Used by the LUCID component (running as normal user) to send commands to the
root-owned helper. Connects to the Unix socket, sends JSON-line requests,
reads JSON-line responses. Reconnects on each call (no long-lived connection).
"""
from __future__ import annotations

import json
import os
import socket
from typing import Any

from .protocol import (
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


def _socket_path() -> str:
    return os.environ.get("LUCID_LED_STRIP_SOCKET", DEFAULT_SOCKET_PATH)


def _request(cmd: str, **params: Any) -> dict:
    path = _socket_path()
    req = {"id": 1, "cmd": cmd, **params}
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(10.0)
        sock.connect(path)
        sock.sendall((json.dumps(req) + "\n").encode("utf-8"))
        buf = b""
        while b"\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                return {"ok": False, "error": "connection closed"}
            buf += chunk
        line = buf.split(b"\n", 1)[0].decode("utf-8")
        return json.loads(line)
    except (FileNotFoundError, ConnectionRefusedError, OSError) as e:
        return {"ok": False, "error": str(e)}
    finally:
        try:
            sock.close()
        except OSError:
            pass


def init(
    strip1_count: int = 896,
    strip2_count: int = 894,
    strip1_pin: int = 18,
    strip2_pin: int = 13,
    brightness: int = 125,
) -> dict:
    """Initialize hardware. Returns {ok: bool, error?: str}."""
    return _request(
        CMD_INIT,
        strip1_count=strip1_count,
        strip2_count=strip2_count,
        strip1_pin=strip1_pin,
        strip2_pin=strip2_pin,
        brightness=brightness,
    )


def reset() -> dict:
    """Stop effect and clear all LEDs."""
    return _request(CMD_RESET)


def stop_effect() -> dict:
    """Stop the current running effect."""
    return _request(CMD_STOP_EFFECT)


def set_brightness(brightness: int) -> dict:
    """Set global brightness 0-255."""
    return _request(CMD_SET_BRIGHTNESS, brightness=brightness)


def clear() -> dict:
    """Clear all LEDs (and stop effect)."""
    return _request(CMD_CLEAR)


def set_color(color: dict[str, int] | None) -> dict:
    """Set all LEDs to color {r,g,b} or white if None."""
    return _request(CMD_SET_COLOR, color=color)


def set_range_percent(color: dict[str, int], start_percent: float, end_percent: float) -> dict:
    """Set a range by percent (0.0-1.0)."""
    return _request(
        CMD_SET_RANGE_PERCENT,
        color=color,
        start_percent=start_percent,
        end_percent=end_percent,
    )


def set_range_exact(
    color: dict[str, int],
    start_index: int,
    end_index: int,
) -> dict:
    """Set a range by LED indices."""
    return _request(
        CMD_SET_RANGE_EXACT,
        color=color,
        start_index=start_index,
        end_index=end_index,
    )


def effect(name: str, params: dict[str, Any] | None = None) -> dict:
    """Start an effect by name with optional params."""
    return _request(CMD_EFFECT, name=name, params=params or {})


def ping() -> dict:
    """Check if helper is reachable and hardware is initialized. Returns {ok: bool, error?: str}."""
    return _request(CMD_PING)


def get_pixels() -> dict:
    """Return current pixel buffer. Returns {ok: bool, pixels?: list of [r,g,b], error?: str}."""
    return _request(CMD_GET_PIXELS)
