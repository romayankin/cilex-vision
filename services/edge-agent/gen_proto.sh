#!/bin/bash
# Generate Python protobuf code from the repo's .proto definitions.
#
# Usage:
#   Local dev:  cd services/edge-agent && bash gen_proto.sh
#   Docker:     (automatically run during image build)
#
# The script detects whether it's running inside Docker (/proto/ exists)
# or locally (falls back to ../../proto/).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

if [ -d "/proto" ]; then
    PROTO_ROOT="/proto"
else
    PROTO_ROOT="${SCRIPT_DIR}/../../proto"
fi

OUT_DIR="${SCRIPT_DIR}/proto_gen"
rm -rf "$OUT_DIR"
mkdir -p "$OUT_DIR"

python -m grpc_tools.protoc \
    -I"$PROTO_ROOT" \
    --python_out="$OUT_DIR" \
    --pyi_out="$OUT_DIR" \
    "$PROTO_ROOT"/vidanalytics/v1/common/common.proto \
    "$PROTO_ROOT"/vidanalytics/v1/frame/frame.proto

# Create __init__.py at each package level for Python imports.
find "$OUT_DIR" -type d -exec touch {}/__init__.py \;

echo "Proto generation complete → $OUT_DIR"
