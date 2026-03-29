# lucid-component-led-strip

LUCID component for WS281x LED strip control on Raspberry Pi. Ported from [led_truss](https://github.com/IERoboticsAILab/led_truss) — a FastAPI service for the OptiTrack truss — into the LUCID agent component model, replacing REST endpoints with MQTT commands.

**Architecture:** The component runs inside **lucid-agent-core** (as user `lucid`) and talks to a separate **LED strip helper daemon** over a Unix socket (`/run/lucid/led-strip.sock`). Only the helper runs as **root** and owns the `rpi_ws281x` hardware (DMA access requires root), so the agent never needs elevated privileges. All IPC uses newline-delimited JSON over a short-lived connection per call.

---

## Hardware

- **Raspberry Pi 4B** (Pi 5 not supported by `rpi-ws281x`)
- **WS2813 LED strips** — two strips treated as a single logical ring
  - Strip 1: 896 LEDs on GPIO18 (Pin 12), PWM channel 0
  - Strip 2: 894 LEDs on GPIO13 (Pin 33), PWM channel 1
  - Total: 1790 LEDs across 30m
- **6× 5V/20A PSUs** — one per 5m segment for power injection
- Common ground between PSUs and Raspberry Pi

---

## Installation

**On Raspberry Pi**

1. **Install via MQTT** — publish to `lucid/agents/<agent_id>/cmd/components/install` with the led_strip payload below. The agent installs the wheel, then runs `pip install lucid-component-led-strip[pi]` so the venv has `rpi_ws281x` for the helper, and registers the component.

2. **Start the helper daemon** — after the MQTT install, run once on the Pi (then restart the agent if it was already running):
   ```bash
   sudo /home/lucid/lucid-agent-core/venv/bin/lucid-agent-core install-led-strip-helper
   ```
   The agent does not run sudo; this command installs and starts the root-owned helper. If your venv path differs, use that path.

3. **Keep lucid-agent-core as user `lucid`** — no root. The component connects to the helper at `/run/lucid/led-strip.sock` (override with `LUCID_LED_STRIP_SOCKET`).

Payload (publish to `lucid/agents/<agent_id>/cmd/components/install`):

```json
{
  "request_id": "<uuid>",
  "component_id": "led_strip",
  "source": {
    "type": "github_release",
    "owner": "YOUR_ORG",
    "repo": "lucid-component-led-strip",
    "version": "1.0.0",
    "sha256": "<sha256-of-wheel>"
  }
}
```

**Development on macOS** — Install without the `[pi]` extra. The component (client only) runs; the helper is not used. Use `make setup-venv` with `--no-deps` and no `rpi-ws281x` for tests and wheel build.

```bash
git clone https://github.com/your-org/lucid-component-led-strip
cd lucid-component-led-strip
make setup-venv
make test
make build
```

---

## Building a release (on your Mac)

1. **Tag and build**
   ```bash
   git tag v1.0.0
   make setup-venv
   make build
   ```
2. **Upload the wheel** from `dist/` to a GitHub release (e.g. attach `lucid_component_led_strip-1.0.0-py3-none-any.whl`).
3. **On the Pi**, install via the agent-core MQTT install command (see above). The agent runs `pip install wheel.whl`; pip installs the wheel and `rpi-ws281x`.

---

## Configuration

Default values match the truss installation. Override via `cmd/cfg/set`:

| Key | Default | Notes |
|-----|---------|-------|
| `brightness` | `125` | Applied at runtime (0–255) |
| `strip1_count` | `896` | Requires component restart |
| `strip2_count` | `894` | Requires component restart |
| `strip1_pin` | `18` | Requires component restart |
| `strip2_pin` | `13` | Requires component restart |
| `log_level` | `ERROR` | Component log level (`DEBUG`/`INFO`/`WARNING`/`ERROR`/`CRITICAL`) |

---

## MQTT Topics

All topics live under `lucid/agents/<agent_id>/components/led_strip/`.

### Retained

| Topic | Description |
|-------|-------------|
| `metadata` | Component ID, version, capabilities |
| `status` | Live status (online/offline) |
| `state` | Hardware state snapshot (see State below) |
| `cfg` | Active configuration |
| `cfg/logging` | Logging configuration |
| `cfg/telemetry` | Telemetry configuration |

### Standard commands

| Topic | Payload | Description |
|-------|---------|-------------|
| `cmd/reset` | `{"request_id": "..."}` | Stop effects, clear all LEDs |
| `cmd/ping` | `{"request_id": "..."}` | Heartbeat check |
| `cmd/clear` | `{"request_id": "..."}` | Turn off all LEDs (result on `evt/clear/result`) |
| `cmd/cfg/set` | `{"request_id": "...", "set": {...}}` | Update `/cfg` keys (brightness, strip counts, pins; brightness is runtime, hardware changes require restart) |
| `cmd/cfg/logging/set` | `{"request_id": "...", "set": {"log_level": "INFO"}}` | Update `/cfg/logging` keys |
| `cmd/cfg/telemetry/set` | `{"request_id": "...", "set": {"pixel_rgb": {...}}}` | Update `/cfg/telemetry` keys |

### Effect commands

All effect commands use topic `cmd/effect/<name>` with payload:

```json
{ "request_id": "uuid", ...effect_params }
```

Results are published (non-retained, QoS 1) to `evt/effect/<name>/result`:

```json
{ "request_id": "uuid", "ok": true, "error": null }
```

| Topic | Parameters | Description |
|-------|-----------|-------------|
| `cmd/clear` | — | Turn off all LEDs. Result: `evt/clear/result` |
| `cmd/set-color` | `color?: {r,g,b}` | Solid color (white if omitted). Result: `evt/set-color/result` |
| `cmd/set-range-percent` | `color, start_percent, end_percent` | Color by percentage range. Result: `evt/set-range-percent/result` |
| `cmd/set-range-exact` | `color, start_index, end_index` | Color by LED index range. Result: `evt/set-range-exact/result` |
| `cmd/effect/glow` | `color?, wait_ms?` | Pulsing glow |
| `cmd/effect/wave` | `color?, cycles?, speed?, wait_ms?` | Moving cosine waves |
| `cmd/effect/color-wipe` | `color?, wait_ms?` | Pixel-by-pixel wipe |
| `cmd/effect/color-fade` | `color_from?, color_to?, wait_ms?, steps?` | Fade between two colors |
| `cmd/effect/sparkle` | `color?, wait_ms?, cumulative?` | Sparkles (omit/null color = random multicolor per spark) |
| `cmd/effect/rainbow` | `wait_ms?` | Rainbow fade |
| `cmd/effect/rainbow-cycle` | `wait_ms?` | Uniform rainbow |
| `cmd/effect/theater-chase` | `color?, wait_ms?` | Theater chase (null/omitted color = rainbow mode) |
| `cmd/effect/running` | `color?, wait_ms?, width?` | Running light |

Brightness is configured only via `cmd/cfg/set` with `set: {"brightness": 0-255}`. There is no `cmd/effect/set-brightness`.

### Telemetry

| Topic | Payload | Description |
|-------|---------|-------------|
| `telemetry/pixel_rgb` | `{"value": [[r,g,b], ...]}` | Current RGB for all pixels (published periodically when enabled) |

---

## State Payload

Published retained on `state`:

```json
{
  "brightness": 125,
  "current_effect": "rainbow",
  "led_count": 1790,
  "strip1_count": 896,
  "strip2_count": 894,
  "strip1_pin": 18,
  "strip2_pin": 13,
  "hardware_initialized": true
}
```

`current_effect` is `null` when no effect is running.

---

## MQTT Examples

```bash
TOPIC="lucid/agents/{agent_id}/components/led_strip"

# Clear all LEDs
mosquitto_pub -t "$TOPIC/cmd/clear" -m '{"request_id":"0"}'

# Set all LEDs to white
mosquitto_pub -t "$TOPIC/cmd/set-color" \
  -m '{"request_id":"1"}'

# Start rainbow cycle
mosquitto_pub -t "$TOPIC/cmd/effect/rainbow-cycle" \
  -m '{"request_id":"2","wait_ms":50}'

# Glow green
mosquitto_pub -t "$TOPIC/cmd/effect/glow" \
  -m '{"request_id":"3","color":{"r":0,"g":255,"b":0},"wait_ms":10}'

# Set brightness via config (not an effect)
mosquitto_pub -t "$TOPIC/cmd/cfg/set" \
  -m '{"request_id":"cfg1","set":{"brightness":200}}'

# Wave with 2 cycles
mosquitto_pub -t "$TOPIC/cmd/effect/wave" \
  -m '{"request_id":"4","color":{"r":100,"g":0,"b":255},"cycles":2,"speed":0.1}'

# Sparkle rainbow/random colors (omit color)
mosquitto_pub -t "$TOPIC/cmd/effect/sparkle" \
  -m '{"request_id":"5","wait_ms":30}'

# Fade black → blue
mosquitto_pub -t "$TOPIC/cmd/effect/color-fade" \
  -m '{"request_id":"7","color_from":{"r":0,"g":0,"b":0},"color_to":{"r":0,"g":0,"b":255},"steps":100}'

# Stop and clear
mosquitto_pub -t "$TOPIC/cmd/reset" \
  -m '{"request_id":"8"}'
```

---

## IPC Architecture

The component and helper communicate over a **Unix socket** (`/run/lucid/led-strip.sock`, overridable via `LUCID_LED_STRIP_SOCKET`). The protocol is newline-delimited JSON:

- **Request:** `{"id": <int>, "cmd": "<command>", ...params}`
- **Response:** `{"id": <same>, "ok": <bool>, "error": "<str>"}`

The client (`client.py`) opens a new socket connection per call (no persistent connection). The helper (`helper_server.py`) handles each connection in a dedicated thread, dispatching commands to `HelperState`.

**Effect lifecycle inside the helper:**

- `EffectOrchestrator` runs exactly one effect thread at a time.
- Starting a new effect calls `orchestrator.stop()` first, which sets a `cancel_event` and joins the previous thread (up to 2 s).
- Effect functions receive `(hardware, cancel_event, **params)` and loop until `cancel_event.is_set()`.
- Single-pass effects (color-wipe, color-fade, sparkle) exit naturally after one pass; the thread finishes and `EffectOrchestrator` clears `_current_effect`.
- Looping effects (glow, wave, rainbow, rainbow-cycle, theater-chase, running) run until stopped.

**Effect parameter conventions:**

- Colors are passed as `{"r": int, "g": int, "b": int}` in MQTT payloads and are unpacked to `r/g/b` kwargs by the helper before calling the effect function.
- `color=null` or omitted in MQTT maps to the multicolor sentinel (`r=g=b=-1`) for sparkle and theater-chase, enabling random or rainbow mode respectively.
- `color_from`/`color_to` in color-fade are unpacked to `r_from/g_from/b_from/r_to/g_to/b_to` kwargs.

---

## Project Layout

```
src/lucid_component_led_strip/
├── component.py          # LEDStripComponent — MQTT command dispatch, talks to helper via client
├── client.py             # IPC client (Unix socket) — reconnects per call, no persistent connection
├── protocol.py           # IPC command/response constants and DEFAULT_SOCKET_PATH
├── helper_server.py      # Root daemon: listens on Unix socket, owns hardware, runs HelperState
├── hardware.py           # LEDStripHardware — wraps two Adafruit_NeoPixel strips as a single ring
├── helper_installer.py   # CLI tool: installs + enables the systemd helper service
├── systemd/
│   └── lucid-led-strip-helper.service
└── effects/
    ├── __init__.py       # EffectOrchestrator (thread lifecycle) + package re-exports
    ├── glow.py           # Pulsing cosine brightness envelope (looping)
    ├── wave.py           # Moving cosine wave using numpy (looping)
    ├── color_wipe.py     # Pixel-by-pixel wipe (single pass)
    ├── color_fade.py     # Linear interpolation between two colors (single pass)
    ├── sparkle.py        # Random pixel sparkles, single-color or multicolor (single pass)
    ├── rainbow.py        # All pixels shift hue together (looping)
    ├── rainbow_cycle.py  # Full rainbow distributed across all pixels (looping)
    ├── theater_chase.py  # Every-third-pixel chase, single-color or rainbow (looping)
    └── running.py        # Single dot chases around the ring (looping)
```

Adding a new effect:
1. Create `effects/<name>.py` with `def run(hardware, cancel_event, **params)`.
2. Import and re-export it in `effects/__init__.py`.
3. Add an `on_cmd_effect_<name>` handler in `component.py`.
4. Add the `"effect/<name>"` entry to `CAPABILITIES` in `component.py`.
5. Add the effect's entry to `effect_map` in `helper_server.py:HelperState.start_effect`.

---

## Development

```bash
make setup-venv   # Create .venv (skips rpi-ws281x on non-Pi)
make build        # Build wheel and sdist
make clean        # Remove build artifacts
```

## Testing

```bash
make test            # Run all tests
make test-unit       # Unit tests only
make test-coverage   # Tests with coverage report
```

Tests stub out `rpi_ws281x` so they run without hardware. For full integration testing, deploy to a Raspberry Pi 4B with strips connected.

---

## Migration from led_truss

| led_truss | lucid-component-led-strip |
|-----------|--------------------------|
| `POST /effects/rainbow-cycle` | `cmd/effect/rainbow-cycle` |
| `POST /control/clear` | `cmd/clear` or `cmd/reset` |
| `POST /control/set-brightness` | `cmd/cfg/set` with `set: {"brightness": n}` |
| `GET /effects` | `metadata` retained topic (capabilities list) |
| Bitcoin effect | Removed (external dependency) |
| Heart rate effect | Removed (Playwright dependency) |
| Home Assistant REST commands | Replace with MQTT commands via HA `mqtt.publish` |

---

## Troubleshooting

**Helper exits with `No module named 'rpi_ws281x'`** — The agent’s MQTT install runs `pip install lucid-component-led-strip[pi]` after the wheel; if that failed or you installed before this was added, on the Pi run:

```bash
sudo /home/lucid/lucid-agent-core/venv/bin/pip install 'lucid-component-led-strip[pi]'
sudo systemctl restart lucid-led-strip-helper
```

---

## Notes

- Start a new effect to automatically stop the previous one.
- `cmd/reset` stops all effects and clears the strip.
- Hardware config changes (`strip1_count`, pins) require a component restart to take effect; the `cfg/set` result will include an error message indicating this.
- Only the **lucid-led-strip-helper** service runs as root; **lucid-agent-core** stays as user `lucid`.
