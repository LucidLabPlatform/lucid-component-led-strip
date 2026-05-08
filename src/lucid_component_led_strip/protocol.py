"""
IPC protocol between LUCID LED strip component (agent) and LED helper daemon (root).

JSON lines over Unix socket. Client sends one JSON object per line; server responds
with one JSON object per line. Connection can carry multiple request/response pairs.

Request:  {"id": <int|str>, "cmd": "<command>", ...params}
Response: {"id": <same>, "ok": <bool>, "error": "<str>" if not ok}
"""
from __future__ import annotations

# Command names (server and client use these)
CMD_INIT = "init"
CMD_RESET = "reset"
CMD_STOP_EFFECT = "stop_effect"
CMD_SET_BRIGHTNESS = "set_brightness"
CMD_CLEAR = "clear"
CMD_SET_COLOR = "set_color"
CMD_SET_RANGE_PERCENT = "set_range_percent"
CMD_SET_RANGE_EXACT = "set_range_exact"
CMD_EFFECT = "effect"
CMD_GET_PIXELS = "get_pixels"

# Default socket path (override with LUCID_LED_STRIP_SOCKET)
DEFAULT_SOCKET_PATH = "/run/lucid/led-strip.sock"
