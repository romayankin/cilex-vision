#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OUT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/proto_gen"

rm -rf "${OUT_DIR}"
mkdir -p "${OUT_DIR}"

python3 -m grpc_tools.protoc \
  -I "${ROOT_DIR}/proto" \
  --python_out="${OUT_DIR}" \
  "${ROOT_DIR}"/proto/vidanalytics/v1/*/*.proto
