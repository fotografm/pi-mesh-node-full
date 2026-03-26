#!/bin/bash
# setup-pi-2.sh
# Run on the Pi AFTER the first reboot (I2C must be active).
# Covers: RTC detection and sync, Python venv, Reticulum config,
#         xterm.js download, sudoers, systemd services.
# After this script completes, reboot the Pi then run setup-pi-3.sh.
set -e

echo "=== Pi Setup Part 2 of 3 ==="

# ---------------------------------------------------------------------------
# Verify I2C is up and RTC is present
# ---------------------------------------------------------------------------
echo "[1/8] Checking I2C and RTC..."
if [ ! -e /dev/i2c-1 ]; then
    echo "ERROR: /dev/i2c-1 not found. Did you reboot after setup-pi-1.sh?"
    exit 1
fi

I2C_OUT=$(sudo i2cdetect -y 1 2>&1)
if echo "$I2C_OUT" | grep -q "68"; then
    echo "    DS3231 found at 0x68"
else
    echo "WARNING: No device at 0x68. Check RTC wiring."
    echo "$I2C_OUT"
fi

# Load driver and register device (needed this boot before dtoverlay takes full effect)
sudo modprobe rtc-ds1307 2>/dev/null || true
if ! ls /sys/bus/i2c/devices/i2c-1/1-0068 &>/dev/null 2>&1; then
    echo ds3231 0x68 | sudo tee /sys/bus/i2c/devices/i2c-1/new_device > /dev/null
fi

# Sync RTC from system time (Pi should have NTP time via ethernet)
echo "    Setting RTC from system clock..."
sudo hwclock -w
echo "    RTC time: $(sudo hwclock -r)"

# ---------------------------------------------------------------------------
# Python venv for Reticulum/LXMF
# ---------------------------------------------------------------------------
echo "[2/8] Creating Python venv..."
if [ ! -d ~/rns-web-venv ]; then
    python3 -m venv ~/rns-web-venv
    echo "    Created ~/rns-web-venv"
else
    echo "    ~/rns-web-venv already exists — skipping creation"
fi

echo "    Installing Python packages (this may take a few minutes)..."
~/rns-web-venv/bin/pip install --quiet \
    rns==1.1.3 \
    LXMF==0.9.3 \
    websockets==16.0 \
    msgpack==1.1.2 \
    "peewee<4.0.0"

echo "    Packages installed:"
~/rns-web-venv/bin/pip show rns LXMF websockets msgpack peewee 2>/dev/null | grep -E "^Name|^Version"

# ---------------------------------------------------------------------------
# Reticulum config
# ---------------------------------------------------------------------------
echo "[3/8] Writing Reticulum config..."
mkdir -p ~/.reticulum

if [ ! -f ~/.reticulum/config ]; then
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
    echo "    Written to ~/.reticulum/config"
else
    echo "    ~/.reticulum/config already exists — skipping (edit manually if needed)"
fi

# ---------------------------------------------------------------------------
# Download xterm.js files
# ---------------------------------------------------------------------------
echo "[4/8] Downloading xterm.js..."

download_if_missing() {
    local dest="$1"
    local url="$2"
    local min_size="$3"
    if [ -f "$dest" ] && [ "$(stat -c%s "$dest")" -gt "$min_size" ]; then
        echo "    $dest already present — skipping"
    else
        echo "    Downloading $dest..."
        curl -s -o "$dest" "$url"
        local size
        size=$(stat -c%s "$dest")
        if [ "$size" -lt "$min_size" ]; then
            echo "ERROR: $dest too small ($size bytes) — download may have failed"
            cat "$dest"
            exit 1
        fi
        echo "    OK ($size bytes)"
    fi
}

download_if_missing ~/xterm.min.js \
    "https://cdn.jsdelivr.net/npm/xterm@5.3.0/lib/xterm.js" \
    100000

download_if_missing ~/xterm.min.css \
    "https://cdn.jsdelivr.net/npm/xterm@5.3.0/css/xterm.css" \
    1000

download_if_missing ~/addon-fit.min.js \
    "https://cdn.jsdelivr.net/npm/xterm-addon-fit@0.8.0/lib/xterm-addon-fit.js" \
    1000

# ---------------------------------------------------------------------------
# sudoers rule for web UI shutdown button
# ---------------------------------------------------------------------------
echo "[5/8] Adding sudoers rule for shutdown button..."
SUDOERS_FILE=/etc/sudoers.d/sdr-shutdown
if [ ! -f "$SUDOERS_FILE" ]; then
    echo "user ALL=(ALL) NOPASSWD: /sbin/shutdown" | sudo tee "$SUDOERS_FILE" > /dev/null
    sudo chmod 440 "$SUDOERS_FILE"
    echo "    Written $SUDOERS_FILE"
else
    echo "    Already present — skipping"
fi

# ---------------------------------------------------------------------------
# systemd services
# ---------------------------------------------------------------------------
echo "[6/8] Installing systemd services..."

sudo tee /etc/systemd/system/rnsd.service > /dev/null << 'EOF'
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

sudo tee /etc/systemd/system/rns-web.service > /dev/null << 'EOF'
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

sudo tee /etc/systemd/system/sdr-ws.service > /dev/null << 'EOF'
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

sudo systemctl daemon-reload
sudo systemctl enable rnsd rns-web sdr-ws
echo "    Services installed and enabled"

# ---------------------------------------------------------------------------
# Check required files are present before starting services
# ---------------------------------------------------------------------------
echo "[7/8] Checking deployed files..."
MISSING=0
for f in ~/sdr-ws ~/rns-web.py ~/rns-index.html; do
    if [ ! -f "$f" ]; then
        echo "    MISSING: $f"
        MISSING=1
    else
        echo "    OK: $f"
    fi
done

if [ "$MISSING" -eq 1 ]; then
    echo ""
    echo "WARNING: Some files missing. Run setup-deploy.sh from the desktop first,"
    echo "then start services manually:"
    echo "  sudo systemctl start rnsd rns-web sdr-ws"
else
    echo "[8/8] Starting services..."
    sudo systemctl start rnsd
    sleep 2
    sudo systemctl start rns-web
    sudo systemctl start sdr-ws
    echo "    Services started"
fi

echo ""
echo "=== Part 2 complete ==="
echo ""
echo "REBOOT NOW:  sudo reboot"
echo "After reboot, reconnect and run:  bash setup-pi-3.sh"
