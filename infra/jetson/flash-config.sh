#!/usr/bin/env bash
# JetPack configuration for Cilex Vision edge deployment.
#
# Run once after flashing the Jetson with JetPack 6.x.
# Sets power mode, fan profile, clock frequencies, and swap.
#
# Targets: Jetson Orin NX 16GB, Jetson AGX Orin 32/64GB
# Requires: root privileges (sudo)
#
# Usage:
#   sudo bash flash-config.sh [--power-mode MAXN|15W|30W]
set -euo pipefail

POWER_MODE="${1:---power-mode}"
POWER_VALUE="${2:-MAXN}"

# Parse arguments
if [[ "$POWER_MODE" == "--power-mode" ]]; then
    POWER_MODE="$POWER_VALUE"
else
    POWER_MODE="MAXN"
fi

echo "=== Cilex Vision — Jetson Edge Configuration ==="
echo "Power mode: $POWER_MODE"

# ------------------------------------------------------------------
# 1. Power mode
# ------------------------------------------------------------------
echo "[1/6] Setting power mode to $POWER_MODE..."
if command -v nvpmodel &>/dev/null; then
    case "$POWER_MODE" in
        MAXN)  nvpmodel -m 0 ;;
        15W)   nvpmodel -m 1 ;;
        30W)   nvpmodel -m 2 ;;
        *)     echo "Unknown power mode: $POWER_MODE (use MAXN, 15W, or 30W)"; exit 1 ;;
    esac
    echo "  Power mode set to $POWER_MODE ($(nvpmodel -q 2>/dev/null || echo 'check nvpmodel'))"
else
    echo "  WARNING: nvpmodel not found (not running on Jetson?)"
fi

# ------------------------------------------------------------------
# 2. Max clock frequencies
# ------------------------------------------------------------------
echo "[2/6] Setting max clock frequencies..."
if [ -f /usr/bin/jetson_clocks ]; then
    jetson_clocks
    echo "  Clocks set to maximum"
else
    echo "  WARNING: jetson_clocks not found"
fi

# ------------------------------------------------------------------
# 3. Fan profile — always on at full speed for sustained inference
# ------------------------------------------------------------------
echo "[3/6] Configuring fan profile (always-on)..."
FAN_PWM="/sys/devices/pwm-fan/target_pwm"
FAN_PWM_ALT="/sys/class/hwmon/hwmon*/pwm1"
if [ -f "$FAN_PWM" ]; then
    echo 255 > "$FAN_PWM"
    echo "  Fan set to max (255)"
elif ls $FAN_PWM_ALT 1>/dev/null 2>&1; then
    for f in $FAN_PWM_ALT; do
        echo 255 > "$f"
    done
    echo "  Fan set to max via hwmon"
else
    echo "  WARNING: Fan control path not found — set fan manually"
fi

# ------------------------------------------------------------------
# 4. Swap configuration (16GB variant needs swap for model loading)
# ------------------------------------------------------------------
echo "[4/6] Configuring swap..."
SWAP_FILE="/var/swapfile"
SWAP_SIZE_GB=8
TOTAL_RAM_KB=$(grep MemTotal /proc/meminfo | awk '{print $2}')
TOTAL_RAM_GB=$((TOTAL_RAM_KB / 1024 / 1024))

if [ "$TOTAL_RAM_GB" -le 16 ]; then
    if [ ! -f "$SWAP_FILE" ]; then
        echo "  Creating ${SWAP_SIZE_GB}GB swap file..."
        fallocate -l "${SWAP_SIZE_GB}G" "$SWAP_FILE"
        chmod 600 "$SWAP_FILE"
        mkswap "$SWAP_FILE"
        swapon "$SWAP_FILE"
        echo "$SWAP_FILE none swap sw 0 0" >> /etc/fstab
        echo "  Swap enabled: ${SWAP_SIZE_GB}GB at $SWAP_FILE"
    else
        echo "  Swap file already exists at $SWAP_FILE"
        swapon "$SWAP_FILE" 2>/dev/null || true
    fi
else
    echo "  RAM: ${TOTAL_RAM_GB}GB — swap not required for AGX Orin"
fi

# ------------------------------------------------------------------
# 5. Docker runtime — ensure NVIDIA runtime is default
# ------------------------------------------------------------------
echo "[5/6] Checking Docker NVIDIA runtime..."
DOCKER_DAEMON="/etc/docker/daemon.json"
if command -v docker &>/dev/null; then
    if [ -f "$DOCKER_DAEMON" ]; then
        if grep -q "nvidia" "$DOCKER_DAEMON"; then
            echo "  NVIDIA runtime already configured"
        else
            echo "  WARNING: NVIDIA runtime not in $DOCKER_DAEMON — add manually:"
            echo '    {"runtimes":{"nvidia":{"path":"nvidia-container-runtime","runtimeArgs":[]}},"default-runtime":"nvidia"}'
        fi
    else
        echo "  Creating Docker daemon config with NVIDIA runtime..."
        mkdir -p /etc/docker
        cat > "$DOCKER_DAEMON" <<'DAEMON_EOF'
{
    "runtimes": {
        "nvidia": {
            "path": "nvidia-container-runtime",
            "runtimeArgs": []
        }
    },
    "default-runtime": "nvidia"
}
DAEMON_EOF
        systemctl restart docker 2>/dev/null || echo "  Restart Docker manually: systemctl restart docker"
    fi
else
    echo "  WARNING: Docker not installed"
fi

# ------------------------------------------------------------------
# 6. Directory setup for edge agent
# ------------------------------------------------------------------
echo "[6/6] Creating edge agent directories..."
mkdir -p /var/lib/edge-agent/buffer
mkdir -p /models
echo "  /var/lib/edge-agent/buffer — NATS message buffer"
echo "  /models — TensorRT engine files"

echo ""
echo "=== Configuration complete ==="
echo "Next steps:"
echo "  1. Copy TensorRT engine to /models/yolov8n-int8.engine"
echo "  2. Copy config.yaml to /app/jetson/config.yaml"
echo "  3. Run: docker run --runtime nvidia -v /models:/models cilex-vision/jetson-edge"
