# lucid-component-led-strip

LUCID component for WS281x LED strip control on Raspberry Pi. Ported from [led_truss](https://github.com/IERoboticsAILab/led_truss) — a FastAPI service for the OptiTrack truss — into the LUCID agent component model, replacing REST endpoints with MQTT commands.

**Architecture:** The component runs inside **lucid-agent-core** (as user `lucid`) and talks to a separate **LED strip helper daemon** over a Unix socket. Only the helper runs as **root** and owns the rpi_ws281x hardware, so the agent never needs root or DMA access.

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

1. **One-time on the Pi:** Allow the agent user to run the helper installer without a password (so the agent can install the helper after a successful component install):
   ```bash
   echo 'lucid ALL=(ALL) NOPASSWD: /home/lucid/lucid-agent-core/venv/bin/lucid-led-strip-helper-installer --install-once' | sudo tee /etc/sudoers.d/lucid-led-strip-helper
   sudo chmod 440 /etc/sudoers.d/lucid-led-strip-helper
   ```
   If your venv path is different, adjust the path in the sudoers file.

2. **Install via MQTT** — publish to `lucid/agents/<agent_id>/cmd/components/install` with the led_strip payload below. The agent installs the wheel, registers the component, and then runs the helper installer (copy unit + `systemctl enable --now lucid-led-strip-helper`). Use a wheel built with the `[pi]` extra so the venv has `lucid-led-strip-helper-installer`; or run `pip install 'lucid-component-led-strip[pi]'` in the agent venv before the first MQTT install.

3. **Keep lucid-agent-core as user `lucid`** — no root. The component connects to the helper at `/run/lucid/led-strip.sock` (override with `LUCID_LED_STRIP_SOCKET`).

Payload (publish to `lucid/agents/<agent_id>/cmd/components/install`):

```json
{
  "request_id": "<uuid>",
  "component_id": "led_strip",
  "version": "1.0.0",
  "entrypoint": "lucid_component_led_strip.component:LEDStripComponent",
  "source": {
    "type": "github_release",
    "owner": "YOUR_ORG",
    "repo": "lucid-component-led-strip",
    "tag": "v1.0.0",
    "asset": "lucid_component_led_strip-1.0.0-py3-none-any.whl",
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
| `logs_enabled` | `false` | Stream logs topic |

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

### Standard commands

| Topic | Payload | Description |
|-------|---------|-------------|
| `cmd/reset` | `{"request_id": "..."}` | Stop effects, clear all LEDs |
| `cmd/ping` | `{"request_id": "..."}` | Heartbeat check |
| `cmd/cfg/set` | `{"request_id": "...", "set": {...}}` | Update configuration |

### Effect commands

All effect commands follow:

```json
{ "request_id": "uuid", ...effect_params }
```

Results are published (non-retained, QoS 1) to `evt/effect/<name>/result`:

```json
{ "request_id": "uuid", "ok": true, "error": null }
```

| Topic | Parameters | Description |
|-------|-----------|-------------|
| `cmd/effect/clear` | — | Turn off all LEDs |
| `cmd/effect/set-color` | `color?: {r,g,b}` | Solid color (white if omitted) |
| `cmd/effect/set-brightness` | `brightness: int` | Set brightness 0–255 |
| `cmd/effect/set-range-percent` | `color, start_percent, end_percent` | Color by percentage range |
| `cmd/effect/set-range-exact` | `color, start_index, end_index` | Color by LED index range |
| `cmd/effect/glow` | `color?, wait_ms?` | Pulsing glow |
| `cmd/effect/wave` | `color?, cycles?, speed?, wait_ms?` | Moving cosine waves |
| `cmd/effect/color-wipe` | `color?, wait_ms?` | Pixel-by-pixel wipe |
| `cmd/effect/color-fade` | `color_from?, color_to?, wait_ms?, steps?` | Fade between two colors |
| `cmd/effect/sparkle` | `color?, wait_ms?, cumulative?` | Sparkles (null color = multicolor) |
| `cmd/effect/rainbow` | `wait_ms?` | Rainbow fade |
| `cmd/effect/rainbow-cycle` | `wait_ms?` | Uniform rainbow |
| `cmd/effect/theater-chase` | `color?, wait_ms?` | Theater chase (null color = rainbow) |
| `cmd/effect/running` | `wait_ms?, width?` | Running light |

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
TOPIC="lucid/agents/zepheros/components/led_strip"

# Set all LEDs to white
mosquitto_pub -t "$TOPIC/cmd/effect/set-color" \
  -m '{"request_id":"1"}'

# Start rainbow cycle
mosquitto_pub -t "$TOPIC/cmd/effect/rainbow-cycle" \
  -m '{"request_id":"2","wait_ms":50}'

# Glow green
mosquitto_pub -t "$TOPIC/cmd/effect/glow" \
  -m '{"request_id":"3","color":{"r":0,"g":255,"b":0},"wait_ms":10}'

# Wave with 2 cycles
mosquitto_pub -t "$TOPIC/cmd/effect/wave" \
  -m '{"request_id":"4","color":{"r":100,"g":0,"b":255},"cycles":2,"speed":0.1}'

# Sparkle multicolor (omit color for random)
mosquitto_pub -t "$TOPIC/cmd/effect/sparkle" \
  -m '{"request_id":"5","wait_ms":30}'

# Set brightness to 200
mosquitto_pub -t "$TOPIC/cmd/effect/set-brightness" \
  -m '{"request_id":"6","brightness":200}'

# Fade black → blue
mosquitto_pub -t "$TOPIC/cmd/effect/color-fade" \
  -m '{"request_id":"7","color_from":{"r":0,"g":0,"b":0},"color_to":{"r":0,"g":0,"b":255},"steps":100}'

# Stop and clear
mosquitto_pub -t "$TOPIC/cmd/reset" \
  -m '{"request_id":"8"}'
```

---

## Project Layout

```
src/lucid_component_led_strip/
├── component.py          # LEDStripComponent — talks to helper via client
├── client.py             # IPC client (Unix socket) used by component
├── protocol.py           # IPC command/response constants
├── helper_server.py      # Root daemon: lucid-led-strip-helper entry point
├── hardware.py           # Low-level WS281x (used only by helper)
├── systemd/
│   └── lucid-led-strip-helper.service
└── effects/
    ├── __init__.py       # EffectOrchestrator + package re-exports
    ├── glow.py
    ├── wave.py
    ├── color_wipe.py
    ├── color_fade.py
    ├── sparkle.py
    ├── rainbow.py
    ├── rainbow_cycle.py
    ├── theater_chase.py
    └── running.py
```

Adding a new effect:
1. Create `effects/<name>.py` with `def run(hardware, cancel_event, **params)`.
2. Import and re-export it in `effects/__init__.py`.
3. Add a `on_cmd_effect_<name>` handler in `component.py`.
4. Register the capability in `EFFECT_CAPABILITIES`.

---

## Testing

```bash
make setup-venv
make test
```

Tests stub out `rpi_ws281x` so they run without hardware. For full integration testing, deploy to a Raspberry Pi 4B with strips connected.

---

## Migration from led_truss

| led_truss | lucid-component-led-strip |
|-----------|--------------------------|
| `POST /effects/rainbow-cycle` | `cmd/effect/rainbow-cycle` |
| `POST /control/clear` | `cmd/effect/clear` or `cmd/reset` |
| `POST /control/set-brightness` | `cmd/effect/set-brightness` |
| `GET /effects` | `metadata` retained topic (capabilities list) |
| Bitcoin effect | Removed (external dependency) |
| Heart rate effect | Removed (Playwright dependency) |
| Home Assistant REST commands | Replace with MQTT commands via HA `mqtt.publish` |

---

## Notes

- Start a new effect to automatically stop the previous one.
- `cmd/reset` stops all effects and clears the strip.
- Hardware config changes (`strip1_count`, pins) require a component restart to take effect; the `cfg/set` result will include an error message indicating this.
- Only the **lucid-led-strip-helper** service runs as root; **lucid-agent-core** stays as user `lucid`.
