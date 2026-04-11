#!/bin/bash
# Generate Python protobuf code for Jetson edge agent.
# Includes detection.proto (not needed by base edge agent).
#
# Usage:
#   Docker:  (automatically run during image build)
#   Local:   cd services/edge-agent/jetson && bash ../../../infra/jetson/gen_proto_jetson.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

if [ -d "/proto" ]; then
    PROTO_ROOT="/proto"
elif [ -d "${SCRIPT_DIR}/../../proto" ]; then
    PROTO_ROOT="${SCRIPT_DIR}/../../proto"
else
    PROTO_ROOT="${SCRIPT_DIR}/../../../proto"
fi

# Output into the base edge agent's proto_gen directory
if [ -d "/app" ]; then
    OUT_DIR="/app/proto_gen"
else
    OUT_DIR="${SCRIPT_DIR}/../../services/edge-agent/proto_gen"
fi

rm -rf "$OUT_DIR"
mkdir -p "$OUT_DIR"

python -m grpc_tools.protoc \
    -I"$PROTO_ROOT" \
    --python_out="$OUT_DIR" \
    --pyi_out="$OUT_DIR" \
    "$PROTO_ROOT"/vidanalytics/v1/common/common.proto \
    "$PROTO_ROOT"/vidanalytics/v1/frame/frame.proto \
    "$PROTO_ROOT"/vidanalytics/v1/detection/detection.proto

# Create __init__.py at each package level for Python imports.
find "$OUT_DIR" -type d -exec touch {}/__init__.py \;

echo "Proto generation complete (with detection.proto) → $OUT_DIR"
