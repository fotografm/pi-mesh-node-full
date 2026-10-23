# pi-mesh-node-full

A self-contained Reticulum/LXMF mesh node on a Raspberry Pi Zero 2W. It combines a LoRa radio (RNode), an RTL-SDR receiver and a set of small web apps, and serves them over its own Wi-Fi hotspot. It needs no internet, no cloud service and no display. Power it up, join its Wi-Fi from a phone or laptop, open a browser, and you get:

- a live **spectrum and waterfall** of the band around the LoRa frequency,
- **LXMF chat** with other Reticulum users over LoRa,
- a list of **nodes announcing** themselves on the mesh,
- a **noise-floor graph** from the RNode,
- a **live node map**,
- a **browser terminal** on the Pi,
- a **landing page** with notes and shutdown/reboot buttons.

Everything runs on the Zero 2W's single board with 425 MB of RAM, using under 20% CPU in normal use. The SDR server is a small static Rust binary, and the rest is plain Python and vanilla HTML/JavaScript, with no frameworks and no build step for the web pages.

---

## Contents

- [How it fits together](#how-it-fits-together)
- [The pieces in detail](#the-pieces-in-detail)
- [Hardware](#hardware)
- [Ports](#ports)
- [Software and versions](#software-and-versions)
- [Repository layout](#repository-layout)
- [Installing](#installing)
- [Changing things](#changing-things)
- [Day-to-day operation](#day-to-day-operation)
- [Reliability: monitoring and watchdog](#reliability-monitoring-and-watchdog)
- [Troubleshooting](#troubleshooting)
- [Security](#security)
- [License](#license)

---

## How it fits together

```
 phone / laptop
      │  Wi-Fi (WPA2 hotspot run by the Pi, Pi = 10.42.0.1)
      ▼
 ┌──────────────────────── Raspberry Pi Zero 2W ────────────────────────┐
 │                                                                      │
 │  landing-server.py  :80     landing page, notes, shutdown/reboot     │
 │                                                                      │
 │  sdr-ws (Rust)      :8080   waterfall page                           │
 │                     :8081   WebSocket: spectrum frames + settings    │
 │        ▲                                                             │
 │        └── RTL-SDR dongle (USB)                                      │
 │                                                                      │
 │  rns-web.py         :8082   LXMF chat + announces page               │
 │                     :8083   WebSocket: live events, noise samples    │
 │                     :8084   combined page (SDR + chat side by side)  │
 │                     :8085   WebSocket: PTY terminal                  │
 │                     :8087   full-page terminal                       │
 │        ▲                                                             │
 │  rns_map.py         :8086   live node map (optional)                 │
 │        ▲                                                             │
 │        └──── rnsd (Reticulum shared instance, transport enabled)     │
 │                 ▲                                                    │
 │                 └── RAK4631 RNode (USB serial /dev/ttyACM0) ── LoRa  │
 └──────────────────────────────────────────────────────────────────────┘
```

- **rnsd** owns the LoRa radio and runs as a *shared instance*. rns-web.py and rns_map.py don't open the radio themselves. They connect to rnsd locally, so the radio settings live in one place (`~/.reticulum/config`).
- **sdr-ws** owns the RTL-SDR dongle. It is completely separate from Reticulum: it shows what's on the air around the LoRa frequency, but doesn't decode it.
- The **web pages** are static files, either served from the Pi's home folder or built into the binary. Live data reaches the browser over WebSockets, so pages update without reloading.
- All URLs use `window.location.hostname`, so the pages also work if you reach the Pi by another IP, for example over a USB Ethernet adapter during setup.

---

## The pieces in detail

### Wi-Fi hotspot (access)

The Pi runs a WPA2 hotspot with NetworkManager (`nmcli device wifi hotspot`). It always has the address **10.42.0.1** and hands out 10.42.0.x addresses to clients. There is no internet routing, and it isn't needed. `setup-pi-1.sh` asks for the SSID and password and sets the hotspot to start at boot.

A desktop can keep its normal wired connection and join the hotspot with a USB Wi-Fi dongle at the same time. Set `ipv4.never-default yes` on that Wi-Fi connection so the desktop's internet stays on the cable.

### Landing page — port 80

`landing-server.py` serves `landing.html` and `notes.html` from the home folder. It has:

- **A button for each service:** the combined view (8084), the node map (8086), notes and the terminal.
- **A clock**, which comes from the Pi's DS3231 real-time clock, because there is no internet time.
- **Quick notes:** a single plain-text notepad, saved in `~/notes.txt` (`GET`/`POST /notes`).
- **Keep-style notes** (`notes.html`): multiple notes with search and highlighted matches, saved in `~/notes.json`. The REST API is `GET`/`POST /api/notes` and `PUT`/`DELETE /api/notes/<id>`.
- **Reboot and Shut Down** buttons, each with a confirmation step (`POST /reboot`, `POST /shutdown`). After a shutdown, the Pi stays off until its power is cycled.

Port 80 is a privileged port. The service runs as `user`, so it needs `CAP_NET_BIND_SERVICE` (see [Installing](#installing)).

### SDR waterfall — sdr-ws (Rust), ports 8080/8081

A single static binary (`main.rs`, about 2.3 MB) that:

1. Reads raw samples from the RTL-SDR at **2 MS/s**. The defaults are a centre frequency of 869.525 MHz and a gain of 33.8 dB.
2. Removes the DC offset, applies a **Hann window**, and computes a **2048-point FFT**. It averages **8 FFTs** per frame, which takes about 8 ms of signal and smooths the noise.
3. Reduces the result to **1024 bins**, each about 2 kHz wide across the 2 MHz span.
4. Sends one frame to every connected browser about **11.7 times a second**. Each frame is one new line of the waterfall.

**The page** (built into the binary from `sdr-index.html`) draws:
- a spectrum trace on top and a scrolling waterfall below,
- a frequency axis,
- a header showing frequency, sample rate and gain.

**The ⚙ settings panel** changes the frequency, sample rate, gain and the dB scales of the spectrum and waterfall. Changes apply to the running SDR immediately. You can save the current settings as a **named preset**, and load or delete presets. Presets are stored in `~/profiles.json`, next to the binary, and the last one used is restored at startup. A **shutdown button** on the page also powers off the Pi.

**Binary frame format** (little-endian, 2056 bytes):

| Offset | Size | Content |
|---|---|---|
| 0 | 4 × i16 | spectrum min, spectrum max, waterfall min, waterfall max (dB × 10) |
| 8 | 1024 × i16 | FFT bins in dBFS × 10, with DC (the centre frequency) at index 512 |

Control messages are JSON text on the same WebSocket: `apply`, `save_preset`, `delete_preset` and `shutdown`.

**Why Rust?** A static musl binary has no runtime dependencies, starts instantly and uses about 7% CPU on the Zero 2W at the current frame rate. The equivalent in Python would need numpy, and would use far more CPU and memory.

> The HTML page is compiled **into** the binary (`include_str!`). Editing `sdr-index.html` on the Pi does nothing: copy it to `src/index.html` and rebuild.

### Reticulum daemon — rnsd

The standard Reticulum daemon, from the Python venv `~/rns-web-venv`. It:

- drives the **RAK4631 RNode** over USB serial (`/dev/ttyACM0`),
- runs as a **shared instance**, so other programs on the Pi can use it,
- has **transport enabled**, so the node relays traffic for others on the mesh.

The radio settings must match the network you want to talk to. raspi20 uses the Dutch/Munich community settings:

| Setting | Value |
|---|---|
| Frequency | 869.463 MHz |
| Bandwidth | 125 kHz |
| Spreading factor | 8 |
| Coding rate | 5 |
| TX power | 20 dBm |

`reticulum/config.example` shows the layout of the config file. Put your own values in `~/.reticulum/config`, then restart `rnsd` and `rns-web`.

### LXMF chat and announces — rns-web.py, ports 8082/8083

A Python bridge between Reticulum/LXMF and the browser:

- **LXMF identity:** created on first run in `~/lxmf-storage/identity`. This file *is* the node's address on the mesh. Back it up.
- **Announces:** the node announces itself as `raspi20` every 15 minutes. The **📡 Announce Now** button sends one immediately.
- **Announce list:** nearby nodes as they announce, with hop count and a marker for unread messages.
- **Conversations:**
  - Send and receive LXMF messages. Delivery receipts update each message's status.
  - The history is kept in SQLite (`~/messages.db`), so it survives restarts.
  - **Hash deduplication:** MeshChat nodes can appear under several LXMF addresses, and these are merged into one conversation.
  - **Name sanitisation:** garbage bytes in announced display names are stripped before display.
- **Noise floor:** once a second, `rnstatus` is queried for the RNode's noise-floor reading. The result is pushed to the browser as a `noise_sample` event.

The WebSocket on 8083 carries live events: announces, messages, receipts and noise samples. It also takes commands: get announces, get conversations, get one conversation, send a message, announce, and get identity.

### Combined view — port 8084

A single page for a laptop screen. On the left are the SDR waterfall and the node map, and on the right are LXMF chat and a **5-minute RNode noise-floor graph** (Chart.js). The noise graph sits right next to the waterfall, so you can tell whether a rise in the RNode's noise floor matches something visible on the waterfall.

### Terminal — ports 8085/8087

A real browser terminal (xterm.js 5.3.0 with the fit add-on) connected to a **PTY** on the Pi. Full-screen programs such as `htop`, `nano`, `vim` and `cmatrix` work properly. Port 8087 is a full-page terminal, and the combined view embeds the same terminal.

Only **one terminal session** at a time is allowed. A second browser gets "Terminal in use elsewhere" instead of fighting over the same shell.

The xterm.js files are downloaded once during setup into the home folder, so the terminal works without internet.

### Node map — rns_map.py, port 8086 (optional)

A live "dartboard" map of the Reticulum network as seen from this node. It uses four separate announce handlers, so each node is classified correctly:

| Aspect | Node type |
|---|---|
| `lxmf.delivery` | LXMF users |
| `lxmf.propagation` | propagation nodes |
| `nomadnetwork.node` | NomadNet nodes |
| `call.audio` | audio-call nodes |

- Nodes and per-minute activity counts are stored in SQLite (`~/rns-map/nodes.db`), with 24 hours of history (`GET /activity`).
- `POST /reset` clears the map.
- It uses aiohttp for HTTP and WebSocket.

> On raspi20 the map is currently **disabled** (`systemctl disable --now rns-map`), as a test while a memory-related hang is investigated. It's the largest Python process (about 35 MB). Everything else works without it. `sudo systemctl enable --now rns-map` brings it back.

---

## Hardware

| Part | Notes |
|---|---|
| Raspberry Pi Zero 2W | 4 × Cortex-A53, 512 MB (about 425 MB usable). Runs 32-bit Raspberry Pi OS **Bookworm** Lite. |
| Waveshare 4-port USB hub HAT | The Zero has only one USB port, and the RNode, the SDR and an optional Ethernet adapter all need one. |
| RAK4631 with RNode firmware | The LoRa radio. Appears as `/dev/ttyACM0`. |
| NESDR SMArt v5 (RTL-SDR) | The receiver for the waterfall. The kernel's DVB driver is blacklisted so sdr-ws can use it. |
| DS3231 RTC (I2C, address 0x68) | Keeps the correct time without internet. Message timestamps and logs depend on it. |
| USB Ethernet adapter (RTL8152) | Optional, used only during setup. |
| Power supply | Use a good 5 V / 2.5 A supply. The radio and SDR both draw from the Pi's USB, and a weak supply causes undervoltage resets. |

---

## Ports

| Port | Protocol | Service | What it is |
|---|---|---|---|
| 80 | HTTP | landing-server.py | Landing page, notes, reboot/shutdown |
| 8080 | HTTP | sdr-ws | Waterfall page |
| 8081 | WebSocket | sdr-ws | Binary spectrum frames + JSON settings |
| 8082 | HTTP | rns-web.py | LXMF/announces page, xterm.js files |
| 8083 | WebSocket | rns-web.py | Live LXMF events + noise samples |
| 8084 | HTTP | rns-web.py | Combined page |
| 8085 | WebSocket | rns-web.py | PTY terminal |
| 8086 | HTTP + WS | rns_map.py | Node map (optional) |
| 8087 | HTTP | rns-web.py | Full-page terminal |

---

## Software and versions

| Component | Version |
|---|---|
| OS | Raspberry Pi OS Bookworm Lite, 32-bit (armhf) |
| Reticulum (`rns`) | `setup-pi-2.sh` installs 1.1.3. Upgraded to and running **1.5.7** with no code changes. |
| LXMF | `setup-pi-2.sh` installs 0.9.3. Upgraded to and running **1.2.1** with no code changes. |
| Other Python packages | websockets 16.0, msgpack 1.1.2, peewee 3.x, aiohttp 3.9 (node map), all in `~/rns-web-venv` |
| Rust (sdr-ws) | rtl-sdr-rs 0.1, rustfft 6, tokio 1, tokio-tungstenite 0.27, serde. Built for `arm-unknown-linux-musleabihf` (ARMv6 + VFP, static) |
| Web libraries | xterm.js 5.3.0 + fit add-on (downloaded at setup), Chart.js 4.4.0 |

---

## Repository layout

| File | Purpose |
|---|---|
| `main.rs` | Rust SDR server source. Goes to `~/sdr-ws/src/main.rs` on the build machine. |
| `Cargo.toml` | Rust dependencies |
| `sdr-index.html` | Waterfall page. Copy to `~/sdr-ws/src/index.html` before building, because it's embedded in the binary. |
| `profiles.json` | Example SDR presets |
| `rns-web.py` | LXMF/announces/noise/terminal/combined-page server |
| `rns-index.html` | LXMF chat and announces page |
| `landing-server.py`, `landing.html` | Port 80 landing page and its server |
| `notes.html` | Keep-style notes app |
| `rns-map/` | Node map: `rns_map.py`, its page in `static/`, `rns-map.service` |
| `reticulum/config.example` | Reticulum config layout |
| `systemd/` | Service units: `rnsd`, `rns-web`, `sdr-ws`, `landing`, `rns-map`, and the monitoring units |
| `healthlog`, `systemd/healthlog.{service,timer}` | 5-minute health log (see [Reliability](#reliability-monitoring-and-watchdog)) |
| `systemd/journald-persist.conf`, `systemd/watchdog.conf` | Persistent journal and hardware watchdog |
| `install_rns_web.sh` | Installs the LXMF bridge on its own |
| `setup-desktop.sh` | Rust cross-compile toolchain on an Ubuntu/Mint desktop |
| `setup-pi-1.sh` | Pi setup, part 1: packages, terminal mouse fix, SDR driver blacklist and udev rule, I2C, RTC overlay, hotspot |
| `setup-pi-2.sh` | Pi setup, part 2 (after a reboot): RTC check, Python venv, Reticulum config, xterm.js, shutdown sudo rule, services |
| `setup-pi-3.sh` | Checks: RTC, services, ports, xterm.js, SDR dongle, LXMF identity |
| `setup-deploy.sh` | Copies the built binary and files from the desktop to the Pi |
| `SETUP.md` | The complete step-by-step build guide |

---

## Installing

`SETUP.md` has every command. In outline:

1. **Desktop: build tools** (`setup-desktop.sh`, SETUP.md section 1). Install rustup, the `arm-unknown-linux-musleabihf` target, the musl cross-compiler from musl.cc, `gcc-arm-linux-gnueabihf` and the libusb headers. Point cargo's linker at the musl gcc in `~/.cargo/config.toml`.
2. **Desktop: build sdr-ws** (section 2):
   ```
   mkdir -p ~/sdr-ws/src
   cp Cargo.toml ~/sdr-ws/
   cp main.rs ~/sdr-ws/src/main.rs
   cp sdr-index.html ~/sdr-ws/src/index.html
   cd ~/sdr-ws
   cargo build --release --target arm-unknown-linux-musleabihf
   ```
   Build on a desktop, never on the Pi. A Rust build needs far more than 425 MB of RAM.
3. **Flash the Pi** with Raspberry Pi OS **Bookworm** Lite 32-bit, hostname and user `user`, SSH enabled. Don't use Trixie: its headless Wi-Fi/SSH setup in Raspberry Pi Imager is broken.
4. **Pi, part 1** (`setup-pi-1.sh`): updates, the RTL-SDR blacklist and udev rule, I2C and the RTC overlay, and the hotspot. Then reboot.
5. **Pi, part 2** (`setup-pi-2.sh`): the RTC check, the Python venv with Reticulum and LXMF, the Reticulum config, xterm.js files, the sudo rule for the shutdown buttons, and the systemd services.
6. **Deploy** (`setup-deploy.sh`): copy the binary, Python and HTML files to the Pi, then start the services.
7. **Port 80 permission for the landing page.** A Python update can drop file capabilities, so set it in the unit instead:
   ```
   sudo systemctl edit landing
   ```
   and add:
   ```
   [Service]
   AmbientCapabilities=CAP_NET_BIND_SERVICE
   ```
8. **Monitoring and watchdog** (SETUP.md section 12a). Recommended.
9. **Check** (`setup-pi-3.sh`, SETUP.md section 14). Join the hotspot and open `http://10.42.0.1`.
10. **Back up `~/lxmf-storage/identity`.** It's your mesh address and cannot be regenerated.

---

## Changing things

| What | Where | Needs |
|---|---|---|
| Waterfall speed | `SEND_INTERVAL_MS` in `main.rs`. 80 ms gives about 11.7 lines/s, and 40 ms gave about 22.7. A slower speed shows more history on screen and uses less CPU. | Rebuild sdr-ws |
| Waterfall page look or controls | `sdr-index.html` → `src/index.html` | Rebuild sdr-ws |
| FFT size, averaging, number of bins | `FFT_SIZE`, `FFT_AVERAGES`, `OUTPUT_BINS` in `main.rs` | Rebuild. The page also has `OUTPUT_BINS = 1024`, so change both. |
| SDR frequency, gain, scales | The ⚙ panel on the waterfall page, saved as presets in `~/profiles.json` | Nothing |
| LoRa radio settings | `~/.reticulum/config` | `sudo systemctl restart rnsd rns-web` |
| Node name, announce interval | `DISPLAY_NAME`, `ANNOUNCE_INTERVAL` in `rns-web.py` | `sudo systemctl restart rns-web` |
| Landing page buttons | `landing.html` | Reload the page |

**Deploying a rebuilt sdr-ws, keeping the old one:**
```
scp target/arm-unknown-linux-musleabihf/release/sdr-ws user@10.42.0.1:sdr-ws.new
ssh user@10.42.0.1
cp -p ~/sdr-ws ~/sdr-ws.bak
sudo systemctl stop sdr-ws
mv ~/sdr-ws.new ~/sdr-ws
sudo systemctl start sdr-ws
```

---

## Day-to-day operation

**Services:**
```
sudo systemctl status rnsd rns-web sdr-ws landing
sudo systemctl restart rns-web
journalctl -u rns-web -f          # follow a service's log
~/rns-web-venv/bin/rnstatus       # radio and interface status
```

**Updates without internet.** The Pi has no route to the internet, so apt can borrow a desktop's connection through SSH instead:
```
ssh -R 1080 user@10.42.0.1
```
Then, on the Pi in that session:
```
P="-o Acquire::http::Proxy=socks5h://127.0.0.1:1080"
P="$P -o Acquire::https::Proxy=socks5h://127.0.0.1:1080"
sudo apt-get $P update
sudo apt-get $P full-upgrade
```
`ssh -R 1080` with no target turns the SSH connection into a SOCKS proxy that the Pi can use. It stops when you close the session, and no routing or firewall changes are needed.

**Backups.** Back up settings, not the whole SD card:
- `~/lxmf-storage/`, `~/.reticulum/config`, `~/messages.db`, `~/profiles.json`, `~/notes.json`, `~/notes.txt`
- `pip freeze` from the venv

Don't `dd` the SD card over Wi-Fi. It loads the Pi so heavily that it hangs.

---

## Reliability: monitoring and watchdog

A Pi Zero 2W running all of this has little memory to spare, and it has hung before. The nasty part is that the Wi-Fi chip keeps broadcasting the SSID even when Linux has frozen. The hotspot looks fine, but nobody can join it. Three small additions (SETUP.md section 12a) make sure a hang leaves evidence and recovers by itself:

| Addition | File | What it does |
|---|---|---|
| Persistent journal | `systemd/journald-persist.conf` → `/etc/systemd/journald.conf.d/persist.conf` | Logs survive reboots. They're capped at 20 MB to spare the SD card and written every 30 s. |
| Hardware watchdog | `systemd/watchdog.conf` → `/etc/systemd/system.conf.d/watchdog.conf` | The BCM2835 watchdog reboots the Pi within about 15 s if it locks up. |
| Health log | `healthlog`, `systemd/healthlog.{service,timer}` | Every 5 minutes, adds one line to `/var/log/healthlog.log` (about 2 months are kept). |

A health log line looks like this:
```
2026-10-23 17:51:16 memavail=292M swapused=0M temp=61.2'C throttled=0x0 load=2.06 0.67 0.23 rnsd=25M rns-web=30M rns-map=0M landing=15M sdr-ws=1M
```

**After an unexpected restart:**
```
journalctl --list-boots           # an extra boot = it restarted
last -x | head                    # a boot with no shutdown before it = a hang
journalctl -b -1 -e               # the end of the boot before the restart
tail -30 /var/log/healthlog.log   # the readings leading up to it
```

**Reading the health log:**
- **Memory leak:** `memavail` falls and `swapused` rises over time. Compare the per-service sizes to see which one grew.
- **Heat:** `temp` climbs towards 80 °C.
- **Power:** `throttled` isn't `0x0`. `0x50005` means undervoltage now and since boot.

A press of a Shutdown button isn't a crash. It shows in the previous boot's journal as `POST /shutdown` (landing page) or `[CMD] Shutdown requested by client` (waterfall page).

---

## Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| The hotspot is visible at full signal, but devices can't join or get no address | The Pi has hung, and the Wi-Fi chip is still broadcasting by itself. With the watchdog on, it reboots itself. Without it, power-cycle the Pi, then check the logs as above. |
| A desktop keeps showing "connection failed" | Same as above. The desktop retries every ~25 s while the Pi isn't answering. |
| The landing page (port 80) doesn't load, but other ports do | The landing service lost its permission to use port 80. Add the `AmbientCapabilities=CAP_NET_BIND_SERVICE` override (see [Installing](#installing)). |
| The waterfall is blank or frozen | Check `journalctl -u sdr-ws`. Look for "Failed to open" (dongle missing, or held by the kernel DVB driver: check the blacklist) or a "Read error" (USB problem). `sudo systemctl restart sdr-ws`. |
| No announces and no messages | Run `~/rns-web-venv/bin/rnstatus`. Check that the RNode interface is up and the radio settings match your network. Restart `rnsd`, then `rns-web`. |
| "Terminal in use elsewhere" | Another browser tab has the terminal open. Only one session at a time is allowed. Close the other one. |
| An edit to `sdr-index.html` on the Pi has no effect | The page is built into the binary. Rebuild on the desktop. |
| The clock is wrong after power-up | The RTC isn't loaded. Check `dtoverlay=i2c-rtc,ds3231` in `/boot/firmware/config.txt` and run `sudo hwclock -r`. |

---

## Security

This is built for a private, isolated hotspot, and **the hotspot password is the only protection**:

- No page requires a login.
- Anyone who joins the Wi-Fi can open the **terminal**, and get a shell as `user` (who has sudo).
- Anyone who joins can also **shut down or reboot** the Pi.

Use a strong WPA2 password, and don't connect the Pi to a network you don't control. If it ever needs to be on a shared network, put authentication in front of the web services, or turn off the terminal and the shutdown endpoints first.

---

## License

MIT
