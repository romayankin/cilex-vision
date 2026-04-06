#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SERVICE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT_DIR="${SERVICE_DIR}/proto_gen"

mkdir -p "${OUT_DIR}"

python3 -m grpc_tools.protoc \
  -I "${ROOT_DIR}/proto" \
  --python_out="${OUT_DIR}" \
  "${ROOT_DIR}"/proto/vidanalytics/v1/common/common.proto \
  "${ROOT_DIR}"/proto/vidanalytics/v1/detection/detection.proto
