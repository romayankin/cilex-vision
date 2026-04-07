#!/usr/bin/env bash
# Add a camera to the pilot deployment.
#
# Usage:
#   bash scripts/pilot/add-camera.sh --id cam-5 --url rtsp://admin:pass@192.168.1.104/stream1
#   bash scripts/pilot/add-camera.sh --id cam-5 --url rtsp://... --name "Rear Entrance"
#
# What it does:
#   1. Tests RTSP connectivity (ffprobe or OpenCV)
#   2. Appends to infra/pilot/cameras.yaml
#   3. Registers in TimescaleDB cameras table
#   4. Prints instructions to restart edge-agent

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
CAMERAS_YAML="$REPO_ROOT/infra/pilot/cameras.yaml"
DB_CONTAINER="pilot-timescaledb"
SITE_ID="pilot-site"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC}  $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC}  $*"; }
fail()  { echo -e "${RED}[FAIL]${NC}  $*"; exit 1; }

# ------------------------------------------------------------------
# Parse arguments
# ------------------------------------------------------------------

CAMERA_ID=""
RTSP_URL=""
CAMERA_NAME=""

while [[ $# -gt 0 ]]; do
    case $1 in
        --id)       CAMERA_ID="$2";   shift 2 ;;
        --url)      RTSP_URL="$2";    shift 2 ;;
        --name)     CAMERA_NAME="$2"; shift 2 ;;
        *)          fail "Unknown argument: $1\nUsage: add-camera.sh --id cam-5 --url rtsp://..." ;;
    esac
done

[ -z "$CAMERA_ID" ] && fail "Missing --id"
[ -z "$RTSP_URL" ]  && fail "Missing --url"
[ -z "$CAMERA_NAME" ] && CAMERA_NAME="Camera $CAMERA_ID"

# ------------------------------------------------------------------
# Check for duplicate
# ------------------------------------------------------------------

if grep -q "camera_id: $CAMERA_ID" "$CAMERAS_YAML" 2>/dev/null; then
    fail "Camera '$CAMERA_ID' already exists in $CAMERAS_YAML"
fi

# ------------------------------------------------------------------
# Test RTSP connectivity
# ------------------------------------------------------------------

info "Testing RTSP connectivity: $RTSP_URL"

rtsp_ok=false
if command -v ffprobe >/dev/null 2>&1; then
    if ffprobe -v quiet -rtsp_transport tcp -i "$RTSP_URL" -t 5 2>/dev/null; then
        rtsp_ok=true
    fi
elif python3 -c "import cv2" 2>/dev/null; then
    if python3 -c "
import cv2, sys
cap = cv2.VideoCapture('$RTSP_URL')
ret, _ = cap.read()
cap.release()
sys.exit(0 if ret else 1)
" 2>/dev/null; then
        rtsp_ok=true
    fi
else
    warn "Neither ffprobe nor OpenCV available — skipping RTSP test."
    rtsp_ok=true  # assume ok
fi

if [ "$rtsp_ok" = true ]; then
    info "RTSP stream reachable."
else
    warn "Could not connect to RTSP stream. Camera may be offline or URL may be wrong."
    echo -n "Continue anyway? [y/N] "
    read -r answer
    [ "$answer" != "y" ] && [ "$answer" != "Y" ] && exit 1
fi

# ------------------------------------------------------------------
# Append to cameras.yaml
# ------------------------------------------------------------------

info "Adding to $CAMERAS_YAML"

cat >> "$CAMERAS_YAML" <<EOF

  - camera_id: $CAMERA_ID
    rtsp_url: "$RTSP_URL"
    enabled: true
EOF

info "cameras.yaml updated."

# ------------------------------------------------------------------
# Register in database
# ------------------------------------------------------------------

info "Registering in TimescaleDB..."

docker exec "$DB_CONTAINER" psql -U cilex -d vidanalytics -c "
INSERT INTO cameras (camera_id, site_id, name, status)
VALUES ('$CAMERA_ID', '$SITE_ID', '$CAMERA_NAME', 'offline')
ON CONFLICT (camera_id) DO UPDATE SET name = EXCLUDED.name;
" >/dev/null 2>&1 && info "Database updated." || warn "Could not update database (is TimescaleDB running?)."

# ------------------------------------------------------------------
# Done
# ------------------------------------------------------------------

echo ""
info "Camera '$CAMERA_ID' added."
echo ""
echo "  To activate, restart the edge agent:"
echo "    docker restart pilot-edge-agent"
echo ""
