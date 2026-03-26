# SDR + RNS Mesh Web Interface — Complete Setup Guide
#
# Hardware:
#   Desktop:  Ubuntu 24.04 (cross-compilation host), username: user
#   Pi:       Raspberry Pi Zero 2W, hostname: raspi20, username: user
#             Waveshare 4-port USB hub HAT
#             NESDR SMArt v5 RTL-SDR dongle
#             RTL8152 USB ethernet adapter
#             RAK4631 RNode on /dev/ttyACM0
#             DS3231 RTC on I2C
#
# Ports served by Pi:
#   8080  HTTP  — SDR waterfall page (Rust binary)
#   8081  WS    — SDR binary spectrum frames (Rust binary)
#   8082  HTTP  — LXMF/announces page + static xterm.js files (Python)
#   8083  WS    — LXMF/announces live events (Python)
#   8084  HTTP  — Combined iframe page, SDR left + LXMF right (Python)
#   8085  WS    — PTY terminal (Python)
#
# Files:
#   sdr-ws binary        ~/sdr-ws          (on Pi)
#   profiles.json        ~/profiles.json   (on Pi, auto-created)
#   rns-web.py           ~/rns-web.py      (on Pi)
#   rns-index.html       ~/rns-index.html  (on Pi)
#   xterm.min.js         ~/xterm.min.js    (on Pi)
#   xterm.min.css        ~/xterm.min.css   (on Pi)
#   addon-fit.min.js     ~/addon-fit.min.js (on Pi)
#   Reticulum config     ~/.reticulum/config (on Pi)

# ===========================================================================
# SECTION 1 — DESKTOP: Install Rust + cross-compilation toolchain
# ===========================================================================

# Install rustup
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
source "$HOME/.cargo/env"

# Add ARM musl target (static binary, no glibc version dependency)
rustup target add arm-unknown-linux-musleabihf

# Install ARM gcc linker
sudo apt install -y gcc-arm-linux-gnueabihf

# Download musl cross-compiler
cd ~
wget https://musl.cc/arm-linux-musleabihf-cross.tgz
tar xzf arm-linux-musleabihf-cross.tgz

# Configure cargo linkers
mkdir -p ~/.cargo
cat >> ~/.cargo/config.toml << 'EOF'
[target.arm-unknown-linux-gnueabihf]
linker = "arm-linux-gnueabihf-gcc"

[target.arm-unknown-linux-musleabihf]
linker = "/home/user/arm-linux-musleabihf-cross/bin/arm-linux-musleabihf-gcc"
EOF

# Install libusb dev headers (needed by rtl-sdr-rs build)
sudo apt install -y libusb-1.0-0-dev pkg-config

# ===========================================================================
# SECTION 2 — DESKTOP: Create and build the Rust SDR project
# ===========================================================================

# Create project
mkdir -p ~/sdr-ws/src
cd ~/sdr-ws

# Copy in the project files (from this repo/backup)
# Cargo.toml -> ~/sdr-ws/Cargo.toml
# main.rs    -> ~/sdr-ws/src/main.rs
# sdr-index.html -> ~/sdr-ws/src/index.html   (NOTE: must be named index.html)

# Build for Pi Zero 2W (ARM musl static binary)
cargo build --release --target arm-unknown-linux-musleabihf

# Binary output: target/arm-unknown-linux-musleabihf/release/sdr-ws

# ===========================================================================
# SECTION 3 — PI: Flash and initial setup
# ===========================================================================

# Flash Raspberry Pi OS Bookworm Lite 32-bit using Raspberry Pi Imager
# In Imager settings:
#   hostname: raspi20
#   username: user
#   password: (your choice)
#   WiFi SSID + password: (your home network)
#   Enable SSH: yes (use password authentication)
# DO NOT use Trixie — headless WiFi/SSH is broken in Imager for Trixie

# ===========================================================================
# SECTION 4 — PI: SSH in and configure system
# ===========================================================================

# SSH in (replace IP with actual address from router/nmap)
# ssh user@192.168.x.x

# Update system
sudo apt update && sudo apt upgrade -y

# Install dependencies
sudo apt install -y python3 python3-venv python3-pip curl wget

# Fix mouse reporting junk in terminal (caused by htop/nano exiting uncleanly)
# This stops mouse clicks from sending escape sequences as keystrokes
echo "PROMPT_COMMAND='printf \"\e[?1000l\e[?1002l\e[?1003l\e[?1006l\"'" >> ~/.bashrc
source ~/.bashrc

# ===========================================================================
# SECTION 5 — PI: WiFi hotspot setup
# ===========================================================================

# Create hotspot on wlan0 (replace SSID and password)
nmcli device wifi hotspot ifname wlan0 ssid raspi20-hotspot password yourpassword

# Make hotspot autoconnect on boot
nmcli connection modify Hotspot connection.autoconnect yes

# Pi hotspot IP will be 10.42.0.1
# Connected clients will get 10.42.0.x addresses

# ===========================================================================
# SECTION 6 — PI: RTL-SDR setup
# ===========================================================================

# Blacklist DVB kernel modules that claim the RTL-SDR dongle
sudo tee /etc/modprobe.d/blacklist-rtl.conf << 'EOF'
blacklist dvb_usb_rtl28xxu
blacklist dvb_usb_v2
blacklist rtl2832_sdr
blacklist rtl2832
EOF

# udev rule so plugdev group can access the dongle without sudo
sudo tee /etc/udev/rules.d/rtl-sdr.rules << 'EOF'
SUBSYSTEM=="usb", ATTRS{idVendor}=="0bda", ATTRS{idProduct}=="2838", GROUP="plugdev", MODE="0664"
EOF

# Add user to plugdev group
sudo usermod -aG plugdev user

# Reload udev
sudo udevadm control --reload-rules && sudo udevadm trigger

# ===========================================================================
# SECTION 7 — PI: DS3231 RTC setup
# ===========================================================================

# Install I2C tools
sudo apt install -y i2c-tools

# Enable I2C bus
sudo raspi-config nonint do_i2c 0

# Reboot for I2C to become available
sudo reboot
# (reconnect via SSH after reboot)

# Verify RTC is detected at address 0x68
sudo i2cdetect -y 1
# Should show "68" in the grid

# Load the DS3231 driver (kernel module is named rtc-ds1307 but covers DS3231 too)
sudo modprobe rtc-ds1307

# Register the device on the I2C bus
echo ds3231 0x68 | sudo tee /sys/bus/i2c/devices/i2c-1/new_device

# Verify the RTC is readable
sudo hwclock -r

# Set RTC from system clock (Pi must have correct time via NTP first)
sudo hwclock -w

# Read back to confirm
sudo hwclock -r

# Make driver and overlay load automatically on every boot
echo "dtoverlay=i2c-rtc,ds3231" | sudo tee -a /boot/firmware/config.txt
echo "rtc-ds1307" | sudo tee -a /etc/modules

# Reboot and verify both system and RTC time are correct
sudo reboot
# (reconnect via SSH after reboot)
# sudo hwclock -r   — should show correct time
# date              — should match hwclock

# ===========================================================================
# SECTION 8 — PI: Reticulum + LXMF Python venv
# ===========================================================================

# Create venv
python3 -m venv ~/rns-web-venv

# Install pinned dependencies
~/rns-web-venv/bin/pip install \
    rns==1.1.3 \
    LXMF==0.9.3 \
    websockets==16.0 \
    msgpack==1.1.2 \
    peewee==3.17.9

# ===========================================================================
# SECTION 9 — PI: Reticulum config
# ===========================================================================

mkdir -p ~/.reticulum

cat > ~/.reticulum/config << 'EOF'
[reticulum]
  enable_transport = True
  share_instance = Yes
  shared_instance_port = 37428
  instance_control_port = 37429
  panic_on_interface_error = No

[interfaces]

  [[RNode RPI]]
    type = RNodeInterface
    interface_enabled = True
    port = /dev/ttyACM0
    frequency = 869525000
    bandwidth = 250000
    txpower = 14
    spreadingfactor = 10
    codingrate = 5

  [[TCP Bootstrap EU]]
    type = TCPClientInterface
    interface_enabled = False
    target_host = reticulum.betweentheborders.com
    target_port = 4965
EOF

# ===========================================================================
# SECTION 10 — PI: Download xterm.js files
# ===========================================================================

curl -s -o ~/xterm.min.js    https://cdn.jsdelivr.net/npm/xterm@5.3.0/lib/xterm.js
curl -s -o ~/xterm.min.css   https://cdn.jsdelivr.net/npm/xterm@5.3.0/css/xterm.css
curl -s -o ~/addon-fit.min.js https://cdn.jsdelivr.net/npm/xterm-addon-fit@0.8.0/lib/xterm-addon-fit.js

# Verify sizes (should be ~283KB, ~5KB, ~1.5KB)
ls -lh ~/xterm* ~/addon*

# ===========================================================================
# SECTION 11 — PI: sudo rule for shutdown button in web UI
# ===========================================================================

echo "user ALL=(ALL) NOPASSWD: /sbin/shutdown" | sudo tee /etc/sudoers.d/sdr-shutdown
sudo chmod 440 /etc/sudoers.d/sdr-shutdown

# ===========================================================================
# SECTION 12 — PI: systemd services
# ===========================================================================

# rnsd service
sudo tee /etc/systemd/system/rnsd.service << 'EOF'
[Unit]
Description=Reticulum Network Stack Daemon
After=network.target

[Service]
Type=simple
User=user
ExecStart=/home/user/rns-web-venv/bin/rnsd
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

# rns-web service
sudo tee /etc/systemd/system/rns-web.service << 'EOF'
[Unit]
Description=RNS Web Bridge (LXMF + Announces)
After=network.target rnsd.service

[Service]
Type=simple
User=user
ExecStart=/home/user/rns-web-venv/bin/python3 /home/user/rns-web.py
WorkingDirectory=/home/user
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

# sdr-ws service
sudo tee /etc/systemd/system/sdr-ws.service << 'EOF'
[Unit]
Description=SDR WebSocket Spectrum Server
After=network.target

[Service]
Type=simple
User=user
ExecStart=/home/user/sdr-ws
WorkingDirectory=/home/user
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

# Enable and start all services
sudo systemctl daemon-reload
sudo systemctl enable rnsd rns-web sdr-ws
sudo systemctl start rnsd
sudo systemctl start rns-web
sudo systemctl start sdr-ws

# ===========================================================================
# SECTION 13 — DESKTOP: Transfer files to Pi
# ===========================================================================
# Run these from the desktop after building the Rust binary.
# Replace 192.168.177.225 with your Pi's actual ethernet IP.

PI=user@192.168.177.225

# Stop services before replacing binary
ssh $PI "sudo systemctl stop sdr-ws"

# Copy Rust binary
scp ~/sdr-ws/target/arm-unknown-linux-musleabihf/release/sdr-ws $PI:~

# Copy Python server and HTML files
scp ~/Downloads/rns-web.py      $PI:~
scp ~/Downloads/rns-index.html  $PI:~

# Restart services
ssh $PI "sudo systemctl start sdr-ws && sudo systemctl restart rns-web"

# ===========================================================================
# SECTION 14 — VERIFY
# ===========================================================================

# Check all three services are running
ssh $PI "sudo systemctl status sdr-ws rns-web rnsd --no-pager"

# Check xterm.js is being served
ssh $PI "curl -s -o /dev/null -w '%{http_code} %{size_download}\n' http://10.42.0.1:8082/xterm.js"
# Expected: 200 283404

# Connect laptop/phone to Pi hotspot, then open:
#   http://10.42.0.1:8080   — SDR waterfall + terminal
#   http://10.42.0.1:8082   — LXMF chat + announces
#   http://10.42.0.1:8084   — Combined view (both side by side)

# ===========================================================================
# SECTION 15 — FILE INVENTORY (keep all these in sync)
# ===========================================================================
#
# ON DESKTOP (source files):
#   ~/sdr-ws/src/main.rs          Rust SDR server source
#   ~/sdr-ws/src/index.html       SDR browser UI (copy of sdr-index.html)
#   ~/sdr-ws/Cargo.toml           Rust dependencies
#
# ON PI (deployed files):
#   ~/sdr-ws                      Rust binary (compiled from above)
#   ~/profiles.json               SDR presets (auto-created on first run)
#   ~/rns-web.py                  Python LXMF/announces/terminal server
#   ~/rns-index.html              LXMF browser UI
#   ~/xterm.min.js                xterm.js terminal library (downloaded)
#   ~/xterm.min.css               xterm.js CSS (downloaded)
#   ~/addon-fit.min.js            xterm.js FitAddon (downloaded)
#   ~/.reticulum/config           Reticulum interface config
#   ~/lxmf-storage/identity       LXMF identity (auto-created, keep backup)
#   ~/messages.db                 LXMF message history (SQLite)
#
# IMPORTANT: ~/lxmf-storage/identity contains your LXMF address.
# Back it up — losing it means losing your address on the mesh.
# scp user@192.168.177.225:~/lxmf-storage/identity ~/backup-lxmf-identity
