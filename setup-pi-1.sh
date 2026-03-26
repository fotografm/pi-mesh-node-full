#!/bin/bash
# setup-pi-1.sh
# Run on the Pi BEFORE the first reboot.
# Covers: system update, dependencies, I2C enable, RTL-SDR blacklist,
#         udev rules, hotspot, mouse fix.
# After this script completes, reboot the Pi then run setup-pi-2.sh.
set -e

echo "=== Pi Setup Part 1 of 3 ==="

# ---------------------------------------------------------------------------
# System update
# ---------------------------------------------------------------------------
echo "[1/8] Updating system packages..."
sudo apt update && sudo apt upgrade -y

# ---------------------------------------------------------------------------
# Install dependencies
# ---------------------------------------------------------------------------
echo "[2/8] Installing dependencies..."
sudo apt install -y \
    python3 python3-venv python3-pip \
    curl wget \
    i2c-tools \
    network-manager

# ---------------------------------------------------------------------------
# Mouse reporting fix — stops terminal junk from htop/nano unclean exits
# ---------------------------------------------------------------------------
echo "[3/8] Fixing mouse reporting in terminal..."
if ! grep -q "PROMPT_COMMAND.*1000l" ~/.bashrc; then
    printf '\nPROMPT_COMMAND='"'"'printf "\e[?1000l\e[?1002l\e[?1003l\e[?1006l"'"'"'\n' >> ~/.bashrc
    echo "    Added to ~/.bashrc"
else
    echo "    Already present — skipping"
fi

# ---------------------------------------------------------------------------
# RTL-SDR — blacklist DVB kernel modules
# ---------------------------------------------------------------------------
echo "[4/8] Blacklisting DVB kernel modules..."
sudo tee /etc/modprobe.d/blacklist-rtl.conf > /dev/null << 'EOF'
blacklist dvb_usb_rtl28xxu
blacklist dvb_usb_v2
blacklist rtl2832_sdr
blacklist rtl2832
EOF

# ---------------------------------------------------------------------------
# RTL-SDR udev rule
# ---------------------------------------------------------------------------
echo "[5/8] Installing RTL-SDR udev rule..."
sudo tee /etc/udev/rules.d/rtl-sdr.rules > /dev/null << 'EOF'
SUBSYSTEM=="usb", ATTRS{idVendor}=="0bda", ATTRS{idProduct}=="2838", GROUP="plugdev", MODE="0664"
EOF
sudo usermod -aG plugdev user
sudo udevadm control --reload-rules && sudo udevadm trigger

# ---------------------------------------------------------------------------
# Enable I2C for RTC
# ---------------------------------------------------------------------------
echo "[6/8] Enabling I2C..."
sudo raspi-config nonint do_i2c 0

# ---------------------------------------------------------------------------
# DS3231 RTC — dtoverlay and module
# ---------------------------------------------------------------------------
echo "[7/8] Configuring DS3231 RTC overlay..."
if ! grep -q "dtoverlay=i2c-rtc,ds3231" /boot/firmware/config.txt; then
    echo "dtoverlay=i2c-rtc,ds3231" | sudo tee -a /boot/firmware/config.txt
    echo "    Added to /boot/firmware/config.txt"
else
    echo "    Already present — skipping"
fi

if ! grep -q "rtc-ds1307" /etc/modules; then
    echo "rtc-ds1307" | sudo tee -a /etc/modules
    echo "    Added rtc-ds1307 to /etc/modules"
else
    echo "    Already present — skipping"
fi

# ---------------------------------------------------------------------------
# WiFi hotspot
# ---------------------------------------------------------------------------
echo "[8/8] Setting up WiFi hotspot..."
echo ""
echo "    Enter hotspot SSID (e.g. raspi20-hotspot):"
read -r HOTSPOT_SSID
echo "    Enter hotspot password (min 8 chars):"
read -r HOTSPOT_PASS

# Check if hotspot already exists
if nmcli connection show "Hotspot" &>/dev/null; then
    echo "    Hotspot connection already exists — skipping creation"
else
    nmcli device wifi hotspot ifname wlan0 ssid "$HOTSPOT_SSID" password "$HOTSPOT_PASS"
fi
nmcli connection modify Hotspot connection.autoconnect yes
echo "    Hotspot configured: SSID=$HOTSPOT_SSID, autoconnect=yes"

echo ""
echo "=== Part 1 complete ==="
echo ""
echo "REBOOT NOW:  sudo reboot"
echo "After reboot, reconnect via SSH and run:  bash setup-pi-2.sh"
