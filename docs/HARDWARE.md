# LED Strip Hardware Guide

## Physical Configuration

The component manages two WS2813 strips wired as a **single logical ring**:

```
Strip 1 (896 LEDs, GPIO 18)
Direction: →→→→→→→→→→→→→ (pixel 0 to 895)

Strip 2 (894 LEDs, GPIO 13)
Direction: ←←←←←←←←←←←← (reversed, pixel 1789 to 896)

Logical ring index: 0 → 895 → 896 → 1789 → wraps to 0
```

Strip 2 is reversed in software so that the ring flows continuously.

## Wiring

### GPIO Connections

| Strip | GPIO Pin | DMA Channel | Frequency |
|-------|----------|-------------|-----------|
| Strip 1 | GPIO 18 (PWM0) | 10 | 800 kHz |
| Strip 2 | GPIO 13 (PWM1) | 5 | 800 kHz |

Data wire: `GPIO Pin → Level Shifter (3.3V→5V) → WS2813 Data In`

### Power

WS2813 LEDs draw ~60mA per LED at full white (60 × 1790 = **107A total maximum**).

**Requirements:**
- 6× 5V/20A power supplies = 120A capacity
- Power injection at every ~300 LEDs to prevent voltage drop
- **Common ground** between all PSUs and Raspberry Pi (critical — floating ground causes data corruption)

### Power Injection Points

```
Pi GND ──────────────────────────────────────── All PSU GND
         ↓            ↓            ↓
Pi GPIO → Strip1[0]→[300]→[600]→[896]→Strip2[894]→[600]→[300]→[0]
                ↑            ↑            ↑            ↑
               PSU1         PSU2         PSU3    PSU4/5/6
               (5V 20A)    (5V 20A)    (5V 20A)
```

Inject 5V+ and GND at each marked injection point.

## Raspberry Pi Setup

```bash
# Enable SPI and I2C in raspi-config
sudo raspi-config nonint do_spi 0
sudo raspi-config nonint do_i2c 0

# Add pi user to gpio group
sudo usermod -aG gpio $USER

# Install helper daemon (requires root for hardware access)
sudo lucid-led-strip-helper-installer

# Verify helper is running
systemctl status lucid-led-strip-helper
```

## Systemd Service

The helper daemon runs as root and keeps the hardware handle open:

```ini
# /etc/systemd/system/lucid-led-strip-helper.service
[Unit]
Description=LUCID LED Strip Helper Daemon
After=network.target

[Service]
Type=simple
User=root
ExecStart=/usr/local/bin/lucid-led-strip-helper
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
```

## Troubleshooting

**LEDs not responding:**
- Check `systemctl status lucid-led-strip-helper`
- Verify common ground between Pi and PSUs
- Check GPIO pin numbers in config match physical wiring

**Flickering:**
- Check power injection points — voltage drop causes flickering
- Measure voltage at the far end of the strip (should be ≥4.8V)

**Wrong colors:**
- WS2813 uses GRB color order internally; the component converts RGB→GRB automatically

**Pi 5 not supported:**
- `rpi_ws281x` library does not support Pi 5's new GPIO hardware
- Use Pi 4B or earlier
