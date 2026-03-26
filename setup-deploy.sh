#!/bin/bash
# setup-deploy.sh
# Run on the DESKTOP to transfer all files to the Pi.
# Edit PI_IP below to match your Pi's ethernet IP.
set -e

PI_IP="192.168.177.225"
PI_USER="user"
PI="$PI_USER@$PI_IP"

echo "=== Deploy to Pi at $PI_IP ==="

# ---------------------------------------------------------------------------
# Check required files exist on desktop
# ---------------------------------------------------------------------------
echo "[1/4] Checking source files..."

MISSING=0
check_file() {
    if [ ! -f "$1" ]; then
        echo "  MISSING: $1"
        MISSING=1
    else
        echo "  OK: $1"
    fi
}

check_file "$HOME/sdr-ws/target/arm-unknown-linux-musleabihf/release/sdr-ws"
check_file "$HOME/Downloads/rns-web.py"
check_file "$HOME/Downloads/rns-index.html"
# sdr-index.html is compiled into the binary — no need to deploy separately

if [ "$MISSING" -eq 1 ]; then
    echo ""
    echo "ERROR: Missing files. Build the project first with setup-desktop.sh"
    echo "and download rns-web.py + rns-index.html from Claude."
    exit 1
fi

# ---------------------------------------------------------------------------
# Stop sdr-ws before replacing binary
# ---------------------------------------------------------------------------
echo "[2/4] Stopping sdr-ws on Pi..."
ssh "$PI" "sudo systemctl stop sdr-ws"

# ---------------------------------------------------------------------------
# Copy files
# ---------------------------------------------------------------------------
echo "[3/4] Copying files to Pi..."

scp "$HOME/sdr-ws/target/arm-unknown-linux-musleabihf/release/sdr-ws" \
    "$PI:~/sdr-ws"
echo "  Copied: sdr-ws binary"

scp "$HOME/Downloads/rns-web.py" "$PI:~/rns-web.py"
echo "  Copied: rns-web.py"

scp "$HOME/Downloads/rns-index.html" "$PI:~/rns-index.html"
echo "  Copied: rns-index.html"

# ---------------------------------------------------------------------------
# Restart services
# ---------------------------------------------------------------------------
echo "[4/4] Restarting services..."
ssh "$PI" "sudo systemctl start sdr-ws && sudo systemctl restart rns-web"

sleep 3

echo ""
echo "=== Deploy complete ==="
echo ""
ssh "$PI" "sudo systemctl status sdr-ws rns-web --no-pager | grep -E 'Active|●'"
echo ""
echo "Open in browser (on hotspot):"
echo "  http://10.42.0.1:8080  — SDR waterfall"
echo "  http://10.42.0.1:8082  — LXMF chat"
echo "  http://10.42.0.1:8084  — Combined view"
