#!/bin/bash
# setup-pi-3.sh
# Run on the Pi AFTER the second reboot.
# Verifies everything is working correctly.
set -e

echo "=== Pi Setup Part 3 of 3 — Verification ==="

PASS=0
FAIL=0

check() {
    local label="$1"
    local result="$2"
    if [ "$result" = "ok" ]; then
        echo "  ✓ $label"
        PASS=$((PASS + 1))
    else
        echo "  ✗ $label — $result"
        FAIL=$((FAIL + 1))
    fi
}

# ---------------------------------------------------------------------------
# RTC
# ---------------------------------------------------------------------------
echo ""
echo "[1] RTC check..."
RTC_TIME=$(sudo hwclock -r 2>&1)
SYS_TIME=$(date)
echo "    RTC:    $RTC_TIME"
echo "    System: $SYS_TIME"
if sudo hwclock -r &>/dev/null; then
    check "RTC readable" "ok"
else
    check "RTC readable" "hwclock failed — check wiring"
fi

# ---------------------------------------------------------------------------
# Services
# ---------------------------------------------------------------------------
echo ""
echo "[2] Service status..."
for svc in rnsd rns-web sdr-ws; do
    STATE=$(systemctl is-active "$svc" 2>/dev/null)
    if [ "$STATE" = "active" ]; then
        check "$svc running" "ok"
    else
        check "$svc running" "state=$STATE"
    fi
done

# ---------------------------------------------------------------------------
# Ports
# ---------------------------------------------------------------------------
echo ""
echo "[3] Port checks..."
sleep 2  # give services a moment

for port in 8080 8082 8083 8084 8085; do
    if ss -tlnp 2>/dev/null | grep -q ":$port "; then
        check "Port $port listening" "ok"
    else
        check "Port $port listening" "not found — service may have failed"
    fi
done

# ---------------------------------------------------------------------------
# xterm.js served correctly
# ---------------------------------------------------------------------------
echo ""
echo "[4] xterm.js serving check..."
SIZE=$(curl -s -o /dev/null -w "%{size_download}" http://10.42.0.1:8082/xterm.js 2>/dev/null)
if [ "$SIZE" -gt 100000 ] 2>/dev/null; then
    check "xterm.js served (${SIZE} bytes)" "ok"
else
    check "xterm.js served" "unexpected size: $SIZE"
fi

# ---------------------------------------------------------------------------
# RTL-SDR dongle
# ---------------------------------------------------------------------------
echo ""
echo "[5] RTL-SDR dongle..."
if lsusb 2>/dev/null | grep -q "0bda:2838"; then
    check "RTL-SDR dongle detected (0bda:2838)" "ok"
else
    check "RTL-SDR dongle detected" "not found — check USB connection"
fi

if lsmod | grep -q "dvb_usb_rtl28xxu"; then
    check "DVB modules blacklisted" "dvb_usb_rtl28xxu still loaded — reboot may fix"
else
    check "DVB modules blacklisted" "ok"
fi

# ---------------------------------------------------------------------------
# LXMF identity
# ---------------------------------------------------------------------------
echo ""
echo "[6] LXMF identity..."
IDENTITY_FILE=~/lxmf-storage/identity
if [ -f "$IDENTITY_FILE" ]; then
    check "LXMF identity exists" "ok"
    echo "    IMPORTANT: Back up $IDENTITY_FILE — it is your mesh address"
    echo "    From desktop: scp user@<pi-ip>:~/lxmf-storage/identity ~/backup-lxmf-identity"
else
    check "LXMF identity" "not yet created — rns-web may need a moment to start"
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo "================================================"
echo "  Results: $PASS passed, $FAIL failed"
echo "================================================"

if [ "$FAIL" -eq 0 ]; then
    echo ""
    echo "All checks passed. Access from hotspot clients:"
    echo "  http://10.42.0.1:8080  — SDR waterfall + terminal"
    echo "  http://10.42.0.1:8082  — LXMF chat + announces"
    echo "  http://10.42.0.1:8084  — Combined view"
else
    echo ""
    echo "Some checks failed. Check service logs:"
    echo "  sudo journalctl -u sdr-ws  -n 30 --no-pager"
    echo "  sudo journalctl -u rns-web -n 30 --no-pager"
    echo "  sudo journalctl -u rnsd    -n 30 --no-pager"
fi
