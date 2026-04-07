#!/usr/bin/env bash
set -euo pipefail

# Cilex Vision — Pilot Deployment Hardware Check
# Run on Ubuntu 24 to collect specs and validate readiness.

BOLD='\033[1m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
CYAN='\033[0;36m'
NC='\033[0m'

PASS="${GREEN}✓${NC}"
WARN="${YELLOW}⚠${NC}"
FAIL="${RED}✗${NC}"

pass_count=0
warn_count=0
fail_count=0

section() { printf "\n${BOLD}${CYAN}═══ %s ═══${NC}\n" "$1"; }
row()     { printf "  %-32s %s\n" "$1" "$2"; }

check() {
    local label="$1" value="$2" min="$3" unit="$4"
    if (( $(echo "$value >= $min" | bc -l 2>/dev/null || echo 0) )); then
        row "$label" "${value} ${unit} ${PASS}"
        ((pass_count++))
    else
        row "$label" "${value} ${unit} ${FAIL}  (need ≥${min} ${unit})"
        ((fail_count++))
    fi
}

check_warn() {
    local label="$1" value="$2" min="$3" unit="$4"
    if (( $(echo "$value >= $min" | bc -l 2>/dev/null || echo 0) )); then
        row "$label" "${value} ${unit} ${PASS}"
        ((pass_count++))
    else
        row "$label" "${value} ${unit} ${WARN}  (recommend ≥${min} ${unit})"
        ((warn_count++))
    fi
}

printf "${BOLD}Cilex Vision — Pilot Hardware Readiness Check${NC}\n"
printf "Host: $(hostname) | $(date -u '+%Y-%m-%d %H:%M:%S UTC')\n"
printf "OS:   $(lsb_release -ds 2>/dev/null || cat /etc/os-release | grep PRETTY_NAME | cut -d= -f2 | tr -d '\"')\n"
printf "Kernel: $(uname -r)\n"

# ─── CPU ───────────────────────────────────────────────
section "CPU"

cpu_model=$(lscpu | grep "Model name" | sed 's/.*:\s*//')
cpu_cores=$(nproc --all)
cpu_threads=$(lscpu | grep "^CPU(s):" | awk '{print $2}')
cpu_arch=$(lscpu | grep "Architecture" | awk '{print $2}')
cpu_freq_max=$(lscpu | grep "CPU max MHz" | awk '{print $4}' | cut -d. -f1 2>/dev/null || echo "N/A")

row "Model" "$cpu_model"
row "Architecture" "$cpu_arch"
check "Cores" "$cpu_cores" 8 "cores"
row "Threads" "$cpu_threads"
if [[ "$cpu_freq_max" != "N/A" && "$cpu_freq_max" != "" ]]; then
    row "Max frequency" "${cpu_freq_max} MHz"
fi

# ─── RAM ───────────────────────────────────────────────
section "RAM"

total_ram_kb=$(grep MemTotal /proc/meminfo | awk '{print $2}')
total_ram_gb=$(echo "scale=1; $total_ram_kb / 1048576" | bc)
available_ram_kb=$(grep MemAvailable /proc/meminfo | awk '{print $2}')
available_ram_gb=$(echo "scale=1; $available_ram_kb / 1048576" | bc)
swap_total_kb=$(grep SwapTotal /proc/meminfo | awk '{print $2}')
swap_total_gb=$(echo "scale=1; $swap_total_kb / 1048576" | bc)

check "Total RAM" "$total_ram_gb" 32 "GB"
check_warn "Available RAM" "$available_ram_gb" 16 "GB"
row "Swap" "${swap_total_gb} GB"

# ─── GPU ───────────────────────────────────────────────
section "GPU (NVIDIA)"

if command -v nvidia-smi &>/dev/null; then
    gpu_count=$(nvidia-smi --query-gpu=count --format=csv,noheader,nounits | head -1)
    row "GPU count" "$gpu_count"

    gpu_idx=0
    while IFS=, read -r name vram_mb driver_ver cuda_ver gpu_util mem_util temp power_draw power_limit; do
        name=$(echo "$name" | xargs)
        vram_mb=$(echo "$vram_mb" | xargs)
        vram_gb=$(echo "scale=1; $vram_mb / 1024" | bc)
        driver_ver=$(echo "$driver_ver" | xargs)
        cuda_ver=$(echo "$cuda_ver" | xargs)
        gpu_util=$(echo "$gpu_util" | xargs)
        mem_util=$(echo "$mem_util" | xargs)
        temp=$(echo "$temp" | xargs)
        power_draw=$(echo "$power_draw" | xargs)
        power_limit=$(echo "$power_limit" | xargs)

        printf "\n  ${BOLD}GPU ${gpu_idx}:${NC} ${name}\n"
        check "  VRAM" "$vram_gb" 24 "GB"
        row "  Driver" "$driver_ver"
        row "  CUDA" "$cuda_ver"
        row "  Current GPU util" "${gpu_util}%"
        row "  Current VRAM util" "${mem_util}%"
        row "  Temperature" "${temp}°C"
        row "  Power" "${power_draw}W / ${power_limit}W"

        ((gpu_idx++))
    done < <(nvidia-smi --query-gpu=name,memory.total,driver_version,cuda_version,utilization.gpu,utilization.memory,temperature.gpu,power.draw,power.limit --format=csv,noheader,nounits)

    # Check CUDA toolkit
    if command -v nvcc &>/dev/null; then
        cuda_toolkit=$(nvcc --version | grep "release" | sed 's/.*release //' | cut -d, -f1)
        row "CUDA toolkit" "$cuda_toolkit ${PASS}"
        ((pass_count++))
    else
        row "CUDA toolkit" "not found ${WARN}"
        ((warn_count++))
    fi

    # Check TensorRT
    if dpkg -l 2>/dev/null | grep -q tensorrt; then
        trt_ver=$(dpkg -l | grep "libnvinfer[0-9]" | head -1 | awk '{print $3}' | cut -d- -f1 2>/dev/null || echo "installed")
        row "TensorRT" "$trt_ver ${PASS}"
        ((pass_count++))
    elif python3 -c "import tensorrt; print(tensorrt.__version__)" 2>/dev/null; then
        trt_ver=$(python3 -c "import tensorrt; print(tensorrt.__version__)" 2>/dev/null)
        row "TensorRT" "$trt_ver ${PASS}"
        ((pass_count++))
    else
        row "TensorRT" "not found ${WARN}  (needed for Triton model conversion)"
        ((warn_count++))
    fi
else
    printf "  ${FAIL} nvidia-smi not found — no NVIDIA GPU detected or driver not installed\n"
    ((fail_count++))
fi

# ─── STORAGE ───────────────────────────────────────────
section "Storage"

root_total_gb=$(df -BG / | tail -1 | awk '{print $2}' | tr -d 'G')
root_avail_gb=$(df -BG / | tail -1 | awk '{print $4}' | tr -d 'G')
root_fs=$(df -T / | tail -1 | awk '{print $2}')

check "Root partition total" "$root_total_gb" 100 "GB"
check_warn "Root partition available" "$root_avail_gb" 50 "GB"
row "Root filesystem" "$root_fs"

# Check for NVMe devices
if ls /dev/nvme* &>/dev/null; then
    nvme_count=$(ls /dev/nvme[0-9]n[0-9] 2>/dev/null | wc -l)
    row "NVMe devices" "${nvme_count} ${PASS}"
    ((pass_count++))
    for dev in /dev/nvme[0-9]n[0-9]; do
        size=$(lsblk -b -dn -o SIZE "$dev" 2>/dev/null | awk '{printf "%.0f", $1/1073741824}')
        model=$(lsblk -dn -o MODEL "$dev" 2>/dev/null | xargs)
        row "  $dev" "${size} GB — ${model}"
    done
else
    row "NVMe devices" "none found ${WARN}"
    ((warn_count++))
fi

# Check all mount points with significant space
printf "\n  ${BOLD}Mount points:${NC}\n"
df -h -x tmpfs -x devtmpfs -x squashfs 2>/dev/null | tail -n+2 | while read -r line; do
    printf "    %s\n" "$line"
done

# ─── NETWORK ───────────────────────────────────────────
section "Network"

default_iface=$(ip route | grep default | head -1 | awk '{print $5}')
if [[ -n "${default_iface:-}" ]]; then
    link_speed=$(cat /sys/class/net/"$default_iface"/speed 2>/dev/null || echo "unknown")
    mac=$(cat /sys/class/net/"$default_iface"/address 2>/dev/null || echo "unknown")
    ip_addr=$(ip -4 addr show "$default_iface" | grep inet | awk '{print $2}' | head -1)
    row "Default interface" "$default_iface"
    row "IP address" "$ip_addr"
    if [[ "$link_speed" != "unknown" && "$link_speed" -gt 0 ]] 2>/dev/null; then
        check_warn "Link speed" "$link_speed" 1000 "Mbps"
    else
        row "Link speed" "unknown"
    fi
else
    row "Default interface" "none found ${WARN}"
    ((warn_count++))
fi

# List all non-loopback interfaces
printf "\n  ${BOLD}All interfaces:${NC}\n"
for iface in /sys/class/net/*; do
    name=$(basename "$iface")
    [[ "$name" == "lo" ]] && continue
    state=$(cat "$iface/operstate" 2>/dev/null || echo "unknown")
    speed=$(cat "$iface/speed" 2>/dev/null || echo "?")
    printf "    %-16s state=%-6s speed=%s Mbps\n" "$name" "$state" "$speed"
done

# ─── DOCKER ────────────────────────────────────────────
section "Docker"

if command -v docker &>/dev/null; then
    docker_ver=$(docker --version | awk '{print $3}' | tr -d ',')
    row "Docker" "$docker_ver ${PASS}"
    ((pass_count++))

    if docker compose version &>/dev/null; then
        compose_ver=$(docker compose version --short 2>/dev/null || docker compose version | awk '{print $NF}')
        row "Docker Compose" "$compose_ver ${PASS}"
        ((pass_count++))
    else
        row "Docker Compose" "not found ${FAIL}"
        ((fail_count++))
    fi

    # Check NVIDIA container runtime
    if docker info 2>/dev/null | grep -q "nvidia"; then
        row "NVIDIA runtime" "available ${PASS}"
        ((pass_count++))
    elif command -v nvidia-container-cli &>/dev/null; then
        row "NVIDIA runtime" "CLI present, check docker config ${WARN}"
        ((warn_count++))
    else
        row "NVIDIA runtime" "not found ${FAIL}  (install nvidia-container-toolkit)"
        ((fail_count++))
    fi

    # Docker disk usage
    docker_root=$(docker info 2>/dev/null | grep "Docker Root Dir" | awk '{print $NF}')
    if [[ -n "${docker_root:-}" ]]; then
        docker_avail=$(df -BG "$docker_root" 2>/dev/null | tail -1 | awk '{print $4}' | tr -d 'G')
        check_warn "Docker root available" "$docker_avail" 50 "GB"
    fi
else
    printf "  ${FAIL} Docker not installed\n"
    ((fail_count++))
fi

# ─── SOFTWARE DEPENDENCIES ─────────────────────────────
section "Software"

check_cmd() {
    local name="$1" cmd="$2" required="${3:-true}"
    if command -v "$cmd" &>/dev/null; then
        ver=$("$cmd" --version 2>&1 | head -1 || echo "installed")
        row "$name" "$ver ${PASS}"
        ((pass_count++))
    elif [[ "$required" == "true" ]]; then
        row "$name" "not found ${FAIL}"
        ((fail_count++))
    else
        row "$name" "not found ${WARN}"
        ((warn_count++))
    fi
}

check_cmd "Python 3" "python3" true
check_cmd "pip" "pip3" false
check_cmd "Git" "git" true
check_cmd "curl" "curl" true
check_cmd "jq" "jq" false
check_cmd "GStreamer" "gst-launch-1.0" false

# Python packages relevant to the pilot
printf "\n  ${BOLD}Python packages:${NC}\n"
for pkg in pyyaml pydantic fastapi numpy opencv-python-headless ultralytics openpyxl; do
    if python3 -c "import importlib; importlib.import_module('${pkg//-/_}')" 2>/dev/null; then
        printf "    %-30s ${PASS}\n" "$pkg"
    else
        printf "    %-30s ${YELLOW}not installed${NC}\n" "$pkg"
    fi
done

# ─── PORTS ─────────────────────────────────────────────
section "Port Availability (pilot services)"

check_port() {
    local port="$1" service="$2"
    if ss -tlnp 2>/dev/null | grep -q ":${port} "; then
        pid_info=$(ss -tlnp 2>/dev/null | grep ":${port} " | grep -oP 'pid=\K[0-9]+' | head -1)
        proc_name=$(ps -p "$pid_info" -o comm= 2>/dev/null || echo "unknown")
        row "Port ${port} (${service})" "IN USE by ${proc_name} ${WARN}"
        ((warn_count++))
    else
        row "Port ${port} (${service})" "available ${PASS}"
        ((pass_count++))
    fi
}

check_port 4222 "NATS"
check_port 5432 "TimescaleDB"
check_port 5000 "MLflow"
check_port 8080 "Query API"
check_port 8001 "Triton gRPC"
check_port 8002 "Triton HTTP"
check_port 9000 "MinIO"
check_port 9090 "Prometheus"
check_port 3000 "Grafana"
check_port 9092 "Kafka"

# ─── CAMERA NETWORK ───────────────────────────────────
section "Camera Network Reachability"

printf "  Checking common camera subnets...\n"
for subnet in "192.168.1" "192.168.0" "10.0.0" "172.16.0"; do
    gateway="${subnet}.1"
    if ping -c 1 -W 1 "$gateway" &>/dev/null; then
        row "  ${subnet}.0/24 gateway" "reachable ${PASS}"
    else
        row "  ${subnet}.0/24 gateway" "no response"
    fi
done

# Check if any ONVIF devices respond (port 80 on local subnet)
printf "\n  ${BOLD}Tip:${NC} To discover ONVIF cameras, run:\n"
printf "    python3 scripts/camera-compat/probe_camera.py --brand <brand> --model <model> --host <ip> --username admin --password <pass>\n"

# ─── SUMMARY ───────────────────────────────────────────
section "Summary"

total=$((pass_count + warn_count + fail_count))
printf "\n"
printf "  ${GREEN}${pass_count} passed${NC}  |  ${YELLOW}${warn_count} warnings${NC}  |  ${RED}${fail_count} failed${NC}  |  ${total} total checks\n"
printf "\n"

if [[ $fail_count -eq 0 && $warn_count -eq 0 ]]; then
    printf "  ${GREEN}${BOLD}READY — hardware meets all pilot requirements.${NC}\n"
elif [[ $fail_count -eq 0 ]]; then
    printf "  ${YELLOW}${BOLD}MOSTLY READY — review warnings above before deployment.${NC}\n"
else
    printf "  ${RED}${BOLD}NOT READY — resolve ${fail_count} failed check(s) before deployment.${NC}\n"
fi

# ─── EXPORT ────────────────────────────────────────────
REPORT_PATH="${HOME}/cilex-hardware-report-$(date +%Y%m%d-%H%M%S).txt"
{
    echo "Cilex Vision Hardware Report"
    echo "Generated: $(date -u '+%Y-%m-%d %H:%M:%S UTC')"
    echo "Host: $(hostname)"
    echo "OS: $(lsb_release -ds 2>/dev/null || echo 'unknown')"
    echo "Kernel: $(uname -r)"
    echo ""
    echo "CPU: $cpu_model ($cpu_cores cores / $cpu_threads threads)"
    echo "RAM: ${total_ram_gb} GB total / ${available_ram_gb} GB available"
    echo "Swap: ${swap_total_gb} GB"
    echo ""
    if command -v nvidia-smi &>/dev/null; then
        echo "GPU:"
        nvidia-smi --query-gpu=name,memory.total,driver_version,cuda_version --format=csv 2>/dev/null
    else
        echo "GPU: none detected"
    fi
    echo ""
    echo "Storage:"
    df -h -x tmpfs -x devtmpfs -x squashfs 2>/dev/null
    echo ""
    echo "Docker: $(docker --version 2>/dev/null || echo 'not installed')"
    echo "Docker Compose: $(docker compose version 2>/dev/null || echo 'not installed')"
    echo ""
    echo "Result: ${pass_count} passed / ${warn_count} warnings / ${fail_count} failed"
} > "$REPORT_PATH"

printf "\n  Report saved to: ${BOLD}${REPORT_PATH}${NC}\n\n"
