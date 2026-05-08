"""
IPC client for the LED strip helper daemon.

Used by the LUCID component (running as normal user) to send commands to the
root-owned helper. Maintains a persistent Unix-socket connection guarded by a
module-level lock; reconnects transparently on broken pipe / timeout. The
helper's per-connection serve loop is sequential, so a single shared socket
matches its threading model.
"""
from __future__ import annotations

import json
import os
import socket
import threading
from typing import Any, Optional

from .protocol import (
    CMD_CLEAR,
    CMD_EFFECT,
    CMD_GET_PIXELS,
    CMD_INIT,
    CMD_RESET,
    CMD_SET_BRIGHTNESS,
    CMD_SET_COLOR,
    CMD_SET_RANGE_EXACT,
    CMD_SET_RANGE_PERCENT,
    CMD_STOP_EFFECT,
    DEFAULT_SOCKET_PATH,
)

_CONNECT_TIMEOUT_S = 10.0
_REQUEST_TIMEOUT_S = 10.0

_lock = threading.Lock()
_sock: Optional[socket.socket] = None
# Holds bytes received past the first '\n' on a keep-alive read — must be
# preserved across calls or the next response is silently corrupted.
_recv_buf = bytearray()


def _socket_path() -> str:
    return os.environ.get("LUCID_LED_STRIP_SOCKET", DEFAULT_SOCKET_PATH)


def _close_socket() -> None:
    """Close the cached socket and reset the recv buffer. Caller holds _lock."""
    global _sock
    if _sock is not None:
        try:
            _sock.close()
        except OSError:
            pass
        _sock = None
    _recv_buf.clear()


def _connect() -> socket.socket:
    """Open a fresh Unix socket to the helper. Caller holds _lock."""
    global _sock
    path = _socket_path()
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(_CONNECT_TIMEOUT_S)
    sock.connect(path)
    sock.settimeout(_REQUEST_TIMEOUT_S)
    _recv_buf.clear()
    _sock = sock
    return sock


def _send_and_recv(sock: socket.socket, payload: bytes) -> dict:
    """Send a request and read one '\\n'-terminated JSON response.

    Caller holds _lock. Any bytes received past the terminator are stashed in
    _recv_buf for the next call. May raise OSError / socket.timeout /
    json.JSONDecodeError; the public _request handles those.
    """
    sock.sendall(payload)
    while b"\n" not in _recv_buf:
        chunk = sock.recv(4096)
        if not chunk:
            raise ConnectionResetError("helper closed connection")
        _recv_buf.extend(chunk)
    nl_idx = _recv_buf.index(b"\n")
    line = bytes(_recv_buf[:nl_idx])
    del _recv_buf[: nl_idx + 1]
    return json.loads(line.decode("utf-8"))


def _request(cmd: str, **params: Any) -> dict:
    """Send a command, returning the helper's JSON response.

    Reuses a persistent socket; on socket / decode errors, closes and retries
    once with a fresh connection before giving up.
    """
    payload = (json.dumps({"id": 1, "cmd": cmd, **params}) + "\n").encode("utf-8")
    with _lock:
        for attempt in (1, 2):
            try:
                sock = _sock if _sock is not None else _connect()
                return _send_and_recv(sock, payload)
            except (BrokenPipeError, ConnectionResetError, socket.timeout,
                    json.JSONDecodeError, OSError) as e:
                _close_socket()
                if attempt == 2:
                    return {"ok": False, "error": str(e) or e.__class__.__name__}
        # Unreachable — loop always returns or falls into the final branch above.
        return {"ok": False, "error": "request failed"}


def close() -> None:
    """Close the cached socket. Safe to call repeatedly."""
    with _lock:
        _close_socket()


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


def get_pixels() -> dict:
    """Return current pixel buffer. Returns {ok: bool, pixels?: list of [r,g,b], error?: str}."""
    return _request(CMD_GET_PIXELS)
