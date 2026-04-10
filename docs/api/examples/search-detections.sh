#!/usr/bin/env bash
set -euo pipefail

: "${TOKEN:?set TOKEN to the value of the access_token cookie}"

BASE_URL="${BASE_URL:-http://localhost:8000}"

curl -s \
  "${BASE_URL}/detections?camera_id=cam-01&start=2026-04-10T00:00:00Z&end=2026-04-10T23:59:59Z&limit=10" \
  --cookie "access_token=${TOKEN}" | python3 -m json.tool
