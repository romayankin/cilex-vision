#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  SITE_ID=<site-id> STEP_CA_URL=https://ca.cilex.internal STEP_CA_FINGERPRINT=<fp> \
  infra/pki/bootstrap-site.sh

Required environment variables:
  SITE_ID
  STEP_CA_URL
  STEP_CA_FINGERPRINT

Optional environment variables:
  STEP_PROVISIONER=ops-bootstrap
  STEP_PROVISIONER_PASSWORD_FILE=/path/to/provisioner-password
  OUTPUT_DIR=./out/<site-id>
  EDGE_DEPLOY_HOST=<user@host>
  EDGE_DEPLOY_PATH=/etc/cilex/pki/<site-id>
  BRIDGE_SECRET_DIR=./out/<site-id>/bridge-secret

What it generates:
  - site NATS server certificate bundle
  - site edge publisher client certificate bundle
  - site bridge subscriber client certificate bundle
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

for required_cmd in step cp mkdir; do
  if ! command -v "${required_cmd}" >/dev/null 2>&1; then
    echo "missing required command: ${required_cmd}" >&2
    exit 1
  fi
done

: "${SITE_ID:?SITE_ID is required}"
: "${STEP_CA_URL:?STEP_CA_URL is required}"
: "${STEP_CA_FINGERPRINT:?STEP_CA_FINGERPRINT is required}"

STEP_PROVISIONER="${STEP_PROVISIONER:-ops-bootstrap}"
OUTPUT_DIR="${OUTPUT_DIR:-./out/${SITE_ID}}"
EDGE_DEPLOY_PATH="${EDGE_DEPLOY_PATH:-/etc/cilex/pki/${SITE_ID}}"
BRIDGE_SECRET_DIR="${BRIDGE_SECRET_DIR:-${OUTPUT_DIR}/bridge-secret}"
STEP_CONTEXT="cilex-internal"

mkdir -p "${OUTPUT_DIR}/nats-server" "${OUTPUT_DIR}/edge-agent" "${BRIDGE_SECRET_DIR}"

bootstrap_args=(
  --ca-url "${STEP_CA_URL}"
  --fingerprint "${STEP_CA_FINGERPRINT}"
  --context "${STEP_CONTEXT}"
)

step ca bootstrap "${bootstrap_args[@]}" >/dev/null 2>&1 || true

issue_args=(
  --ca-url "${STEP_CA_URL}"
  --fingerprint "${STEP_CA_FINGERPRINT}"
  --provisioner "${STEP_PROVISIONER}"
  --not-after 2160h
)

if [[ -n "${STEP_PROVISIONER_PASSWORD_FILE:-}" ]]; then
  issue_args+=(--provisioner-password-file "${STEP_PROVISIONER_PASSWORD_FILE}")
fi

ROOT_CA_PATH="$(step path)/certs/root_ca.crt"

step ca certificate \
  "nats-${SITE_ID}.edge.cilex.internal" \
  "${OUTPUT_DIR}/nats-server/server.crt" \
  "${OUTPUT_DIR}/nats-server/server.key" \
  "${issue_args[@]}" \
  --san "nats.${SITE_ID}.edge.cilex.internal" \
  --san "nats"

step ca certificate \
  "edge-site-${SITE_ID}" \
  "${OUTPUT_DIR}/edge-agent/client.crt" \
  "${OUTPUT_DIR}/edge-agent/client.key" \
  "${issue_args[@]}"

step ca certificate \
  "bridge-site-${SITE_ID}" \
  "${BRIDGE_SECRET_DIR}/client.crt" \
  "${BRIDGE_SECRET_DIR}/client.key" \
  "${issue_args[@]}"

cp "${ROOT_CA_PATH}" "${OUTPUT_DIR}/nats-server/root_ca.crt"
cp "${ROOT_CA_PATH}" "${OUTPUT_DIR}/edge-agent/root_ca.crt"
cp "${ROOT_CA_PATH}" "${BRIDGE_SECRET_DIR}/root_ca.crt"

cat > "${OUTPUT_DIR}/manifest.env" <<EOF
SITE_ID=${SITE_ID}
NATS_SERVER_CERT=${OUTPUT_DIR}/nats-server/server.crt
NATS_SERVER_KEY=${OUTPUT_DIR}/nats-server/server.key
EDGE_CLIENT_CERT=${OUTPUT_DIR}/edge-agent/client.crt
EDGE_CLIENT_KEY=${OUTPUT_DIR}/edge-agent/client.key
BRIDGE_CLIENT_CERT=${BRIDGE_SECRET_DIR}/client.crt
BRIDGE_CLIENT_KEY=${BRIDGE_SECRET_DIR}/client.key
ROOT_CA_CERT=${ROOT_CA_PATH}
EOF

if [[ -n "${EDGE_DEPLOY_HOST:-}" ]]; then
  if ! command -v ssh >/dev/null 2>&1 || ! command -v scp >/dev/null 2>&1; then
    echo "EDGE_DEPLOY_HOST was set but ssh/scp are unavailable" >&2
    exit 1
  fi

  ssh "${EDGE_DEPLOY_HOST}" "sudo mkdir -p '${EDGE_DEPLOY_PATH}/nats-server' '${EDGE_DEPLOY_PATH}/edge-agent'"
  scp "${OUTPUT_DIR}/nats-server/server.crt" "${EDGE_DEPLOY_HOST}:${EDGE_DEPLOY_PATH}/nats-server/server.crt"
  scp "${OUTPUT_DIR}/nats-server/server.key" "${EDGE_DEPLOY_HOST}:${EDGE_DEPLOY_PATH}/nats-server/server.key"
  scp "${OUTPUT_DIR}/nats-server/root_ca.crt" "${EDGE_DEPLOY_HOST}:${EDGE_DEPLOY_PATH}/nats-server/root_ca.crt"
  scp "${OUTPUT_DIR}/edge-agent/client.crt" "${EDGE_DEPLOY_HOST}:${EDGE_DEPLOY_PATH}/edge-agent/client.crt"
  scp "${OUTPUT_DIR}/edge-agent/client.key" "${EDGE_DEPLOY_HOST}:${EDGE_DEPLOY_PATH}/edge-agent/client.key"
  scp "${OUTPUT_DIR}/edge-agent/root_ca.crt" "${EDGE_DEPLOY_HOST}:${EDGE_DEPLOY_PATH}/edge-agent/root_ca.crt"
fi

echo "Generated PKI bundle for site '${SITE_ID}' in ${OUTPUT_DIR}"
