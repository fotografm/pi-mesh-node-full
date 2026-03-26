#!/bin/bash
# setup-desktop.sh
# Run on Ubuntu 24.04 desktop to install Rust cross-compilation toolchain.
# Safe to run multiple times (idempotent).
set -e

echo "=== SDR-WS Desktop Setup ==="

# ---------------------------------------------------------------------------
# Rust + rustup
# ---------------------------------------------------------------------------
if ! command -v rustup &>/dev/null; then
    echo "[1/6] Installing rustup..."
    curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
    source "$HOME/.cargo/env"
else
    echo "[1/6] rustup already installed — skipping"
fi

source "$HOME/.cargo/env"

# ---------------------------------------------------------------------------
# ARM musl target
# ---------------------------------------------------------------------------
echo "[2/6] Adding ARM musl target..."
rustup target add arm-unknown-linux-musleabihf

# ---------------------------------------------------------------------------
# ARM gcc linker (from apt)
# ---------------------------------------------------------------------------
echo "[3/6] Installing ARM gcc linker..."
sudo apt install -y gcc-arm-linux-gnueabihf libusb-1.0-0-dev pkg-config

# ---------------------------------------------------------------------------
# musl cross-compiler
# ---------------------------------------------------------------------------
MUSL_DIR="$HOME/arm-linux-musleabihf-cross"
if [ ! -d "$MUSL_DIR" ]; then
    echo "[4/6] Downloading musl cross-compiler..."
    wget -q --show-progress -O /tmp/arm-musl.tgz https://musl.cc/arm-linux-musleabihf-cross.tgz
    tar xzf /tmp/arm-musl.tgz -C "$HOME"
    rm /tmp/arm-musl.tgz
else
    echo "[4/6] musl cross-compiler already present — skipping"
fi

# ---------------------------------------------------------------------------
# Cargo config
# ---------------------------------------------------------------------------
echo "[5/6] Configuring cargo linkers..."
mkdir -p "$HOME/.cargo"
CARGO_CFG="$HOME/.cargo/config.toml"

if ! grep -q "arm-unknown-linux-musleabihf" "$CARGO_CFG" 2>/dev/null; then
    cat >> "$CARGO_CFG" << EOF

[target.arm-unknown-linux-gnueabihf]
linker = "arm-linux-gnueabihf-gcc"

[target.arm-unknown-linux-musleabihf]
linker = "$MUSL_DIR/bin/arm-linux-musleabihf-gcc"
EOF
    echo "    Linker config written to $CARGO_CFG"
else
    echo "    Linker config already present — skipping"
fi

# ---------------------------------------------------------------------------
# Build the project
# ---------------------------------------------------------------------------
echo "[6/6] Building sdr-ws..."
cd "$HOME/sdr-ws"

# Ensure src/index.html exists (copy from sdr-index.html if needed)
if [ ! -f src/index.html ]; then
    if [ -f "$HOME/Downloads/sdr-index.html" ]; then
        cp "$HOME/Downloads/sdr-index.html" src/index.html
        echo "    Copied sdr-index.html -> src/index.html"
    else
        echo "ERROR: src/index.html missing. Copy sdr-index.html to ~/sdr-ws/src/index.html first."
        exit 1
    fi
fi

cargo build --release --target arm-unknown-linux-musleabihf

echo ""
echo "=== Build complete ==="
echo "Binary: $HOME/sdr-ws/target/arm-unknown-linux-musleabihf/release/sdr-ws"
echo ""
echo "Next step: run setup-pi-1.sh on the Pi, then deploy with setup-deploy.sh"
