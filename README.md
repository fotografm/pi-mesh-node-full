# pi-mesh-node-full

A full-featured Reticulum/LXMF mesh node running on a Raspberry Pi Zero 2W, combining SDR spectrum monitoring, live mesh node map, LXMF chat, noise floor graphing, and a PTY terminal — all served via a dedicated WiFi hotspot.

## Features

- **SDR Waterfall** — RTL-SDR spectrum and waterfall display at 869.525 MHz via Rust WebSocket server
- **LXMF Chat** — send and receive messages with other Reticulum/LXMF nodes
- **Live Announces** — nearby mesh nodes with hop count, unread message indicators
- **Dartboard Node Map** — live Reticulum network topology visualiser (port 8086)
- **Noise Floor Monitor** — real-time Chart.js graph of RNode RF noise over 5 minutes with interference event markers
- **PTY Terminal** — full xterm.js browser terminal with proper PTY (supports htop, cmatrix, vim etc.)
- **Combined View** — SDR + map on left, LXMF + noise graph on right — single page (port 8084)
- **Landing page** — port 80, with clock, quick notes, buttons for all services, shutdown
- **Keep-style notes** — multi-note app with search and highlighted results
- **Settings and presets** — SDR frequency, gain, bandwidth saved as named profiles
- **Hash deduplication** — merges multiple LXMF addresses from the same MeshChat node
- **Node name sanitisation** — strips binary garbage from announce display names

## Hardware

- Raspberry Pi Zero 2W
- NESDR SMArt v5 RTL-SDR dongle
- RAK4631 RNode (LoRa 869.525 MHz, SF10, 250 kHz BW)
- Waveshare 4-port USB hub HAT
- DS3231 RTC module (I2C)
- USB ethernet adapter (optional, for setup)

## Ports

| Port | Service |
|------|---------|
| 80   | Landing page |
| 8080 | SDR waterfall page (Rust) |
| 8081 | WebSocket — binary spectrum frames |
| 8082 | LXMF/Announces UI + static files (xterm.js) |
| 8083 | WebSocket — live LXMF events + noise samples |
| 8084 | Combined view (SDR + map + LXMF + noise) |
| 8085 | WebSocket — PTY terminal |
| 8086 | Dartboard node map |
| 8087 | Full-page PTY terminal |

## Stack

- **Rust** — RTL-SDR reader, FFT, WebSocket server, binary spectrum protocol
- **Python 3** — `rns==1.1.3`, `LXMF==0.9.3`, `websockets==16.0`, `msgpack==1.1.2`
- **Reticulum Network Stack** — transport and LXMF messaging
- **xterm.js 5.3.0** — browser PTY terminal
- **Chart.js 4.4.0** — noise floor graph
- Vanilla HTML/JS — no framework dependencies

## Files

| File | Purpose |
|------|---------|
| `main.rs` | Rust SDR server source |
| `Cargo.toml` | Rust dependencies |
| `sdr-index.html` | SDR waterfall UI — copy to `src/index.html` before compiling |
| `rns-web.py` | Python LXMF/announces/terminal/noise/combined server |
| `rns-index.html` | LXMF browser UI |
| `landing.html` | Landing page |
| `landing-server.py` | Port 80 HTTP server with notes API and shutdown |
| `landing.service` | systemd unit for landing server |
| `notes.html` | Keep-style multi-note app |
| `SETUP.md` | Complete setup reference |
| `setup-desktop.sh` | Rust toolchain setup on Ubuntu desktop |
| `setup-pi-1.sh` | Pi first-boot setup (pre-reboot) |
| `setup-pi-2.sh` | Pi second-stage setup (post-reboot) |
| `setup-pi-3.sh` | Verification checks |
| `setup-deploy.sh` | Deploy files from desktop to Pi |

## Setup

See `SETUP.md` for complete step-by-step instructions.

**Important:** `sdr-index.html` must be copied to `~/sdr-ws/src/index.html` and recompiled before deploying the Rust binary. See `setup-desktop.sh`.

**Important:** Back up `~/lxmf-storage/identity` from the Pi — this file is your LXMF address on the mesh and cannot be regenerated.

## License

MIT
