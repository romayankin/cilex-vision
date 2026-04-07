#!/usr/bin/env bash
# List all registered cameras with status and RTSP connectivity.
#
# Usage:  bash scripts/pilot/list-cameras.sh

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
DB_CONTAINER="pilot-timescaledb"

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

# ------------------------------------------------------------------
# Query cameras from database
# ------------------------------------------------------------------

echo ""
echo "Registered cameras (from database):"
echo "------------------------------------------------------------"
printf "%-16s %-12s %-30s\n" "CAMERA_ID" "STATUS" "NAME"
echo "------------------------------------------------------------"

db_output=$(docker exec "$DB_CONTAINER" psql -U cilex -d vidanalytics -t -A -F '|' -c \
    "SELECT camera_id, COALESCE(status, 'unknown'), COALESCE(name, '') FROM cameras ORDER BY camera_id" 2>/dev/null || echo "")

if [ -z "$db_output" ]; then
    echo "  (no cameras registered or database not running)"
else
    while IFS='|' read -r cam_id status name; do
        case "$status" in
            online)  color=$GREEN  ;;
            offline) color=$RED    ;;
            *)       color=$YELLOW ;;
        esac
        printf "%-16s ${color}%-12s${NC} %-30s\n" "$cam_id" "$status" "$name"
    done <<< "$db_output"
fi

echo ""

# ------------------------------------------------------------------
# RTSP connectivity checks from cameras.yaml
# ------------------------------------------------------------------

CAMERAS_YAML="$REPO_ROOT/infra/pilot/cameras.yaml"

if [ ! -f "$CAMERAS_YAML" ]; then
    echo "cameras.yaml not found at $CAMERAS_YAML"
    exit 0
fi

echo "RTSP connectivity (from cameras.yaml):"
echo "------------------------------------------------------------"
printf "%-16s %-10s %s\n" "CAMERA_ID" "RTSP" "URL"
echo "------------------------------------------------------------"

# Parse camera entries from YAML (simple grep-based, no yq dependency)
cam_id=""
cam_url=""

while IFS= read -r line; do
    if echo "$line" | grep -q "camera_id:"; then
        cam_id=$(echo "$line" | sed 's/.*camera_id:\s*//' | tr -d '"' | tr -d ' ')
    fi
    if echo "$line" | grep -q "rtsp_url:"; then
        cam_url=$(echo "$line" | sed 's/.*rtsp_url:\s*//' | tr -d '"' | tr -d ' ')

        # Test connectivity
        rtsp_status="SKIP"
        if command -v ffprobe >/dev/null 2>&1; then
            if timeout 5 ffprobe -v quiet -rtsp_transport tcp -i "$cam_url" 2>/dev/null; then
                rtsp_status="${GREEN}OK${NC}"
            else
                rtsp_status="${RED}FAIL${NC}"
            fi
        elif python3 -c "import cv2" 2>/dev/null; then
            if python3 -c "
import cv2, sys
cap = cv2.VideoCapture('$cam_url')
ret, _ = cap.read()
cap.release()
sys.exit(0 if ret else 1)
" 2>/dev/null; then
                rtsp_status="${GREEN}OK${NC}"
            else
                rtsp_status="${RED}FAIL${NC}"
            fi
        fi

        printf "%-16s ${rtsp_status}%-10s %s\n" "$cam_id" "" "$cam_url"
        cam_id=""
        cam_url=""
    fi
done < "$CAMERAS_YAML"

echo ""
