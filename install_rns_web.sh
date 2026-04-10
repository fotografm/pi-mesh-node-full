#!/bin/bash
# install_rns_web.sh
# Installs rns-web on Raspberry Pi Zero 2W (Bookworm)
# Run as: bash install_rns_web.sh

set -e
INSTALL_DIR="$HOME"
VENV="$INSTALL_DIR/rns-web-venv"
SERVICE_NAME="rns-web"

echo "=== rns-web install ==="

# 1. System dependencies
echo "[1/5] Installing system packages..."
sudo apt-get update -q
sudo apt-get install -y python3 python3-venv python3-pip libffi-dev

# 2. Python venv
echo "[2/5] Creating Python venv at $VENV..."
python3 -m venv "$VENV"

# 3. Python packages — pinned to match existing MeshChat versions
echo "[3/5] Installing Python packages..."
"$VENV/bin/pip" install --upgrade pip --quiet
"$VENV/bin/pip" install \
    "rns==1.1.3" \
    "LXMF==0.9.3" \
    "peewee<4.0.0" \
    "msgpack==1.1.2" \
    "websockets" \
    --quiet

echo "[3/5] Installed packages:"
"$VENV/bin/pip" show rns LXMF websockets | grep -E "^(Name|Version):"

# 4. Copy program files
echo "[4/5] Installing program files..."
cp rns-web.py    "$INSTALL_DIR/rns-web.py"
cp rns-index.html "$INSTALL_DIR/rns-index.html"
chmod +x "$INSTALL_DIR/rns-web.py"

# 5. systemd service
echo "[5/5] Installing systemd service..."
cat > /tmp/rns-web.service << UNIT
[Unit]
Description=RNS Web Bridge (LXMF + Announces)
After=network.target rnsd.service
Wants=rnsd.service

[Service]
Type=simple
User=user
WorkingDirectory=/home/user
ExecStart=/home/user/rns-web-venv/bin/python3 /home/user/rns-web.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=multi-user.target
UNIT

sudo cp /tmp/rns-web.service /etc/systemd/system/rns-web.service
sudo systemctl daemon-reload
sudo systemctl enable rns-web
sudo systemctl start rns-web

echo ""
echo "=== Done ==="
echo "UI:        http://10.42.0.1:8082"
echo "WebSocket: ws://10.42.0.1:8083"
echo ""
echo "Status: sudo systemctl status rns-web"
echo "Logs:   sudo journalctl -u rns-web -f"
