#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
OUT_DIR="${SCRIPT_DIR}/proto_gen"
mkdir -p "${OUT_DIR}"
if [ -d "/proto/vidanalytics" ]; then
    PROTO_ROOT="/proto"
elif [ -d "${SCRIPT_DIR}/../../proto/vidanalytics" ]; then
    PROTO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)/proto"
else
    echo "Cannot find proto directory" >&2
    exit 1
fi
python3 -m grpc_tools.protoc \
  -I "${PROTO_ROOT}" \
  --python_out="${OUT_DIR}" \
  "${PROTO_ROOT}"/vidanalytics/v1/common/common.proto \
  "${PROTO_ROOT}"/vidanalytics/v1/detection/detection.proto
