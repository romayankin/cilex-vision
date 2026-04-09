#!/usr/bin/env bash
# Generate Python protobuf code from repo .proto files.
# Works both inside Docker (proto at /proto/) and locally (../../proto/).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
OUT_DIR="${SCRIPT_DIR}/proto_gen"

# Detect proto root
if [ -d "/proto/vidanalytics" ]; then
    PROTO_ROOT="/proto"
elif [ -d "${SCRIPT_DIR}/../../proto/vidanalytics" ]; then
    PROTO_ROOT="$(cd "${SCRIPT_DIR}/../../proto" && pwd)"
else
    echo "ERROR: cannot find proto/ directory" >&2
    exit 1
fi

rm -rf "${OUT_DIR}"
mkdir -p "${OUT_DIR}"

python -m grpc_tools.protoc \
    -I "${PROTO_ROOT}" \
    --python_out="${OUT_DIR}" \
    --pyi_out="${OUT_DIR}" \
    $(find "${PROTO_ROOT}/vidanalytics" -name '*.proto')

# Create __init__.py files at each package level
find "${OUT_DIR}" -type d -exec touch {}/__init__.py \;

echo "Proto generation complete: ${OUT_DIR}"
