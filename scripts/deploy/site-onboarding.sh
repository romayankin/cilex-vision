#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  ./site-onboarding.sh --site-id alpha --site-name "Alpha Site" \
      --edge-host 10.44.1.10 --camera-count 10

Optional:
  --nats-host 10.44.1.10         Deploy site-local NATS on a different host
  --db-site-id <uuid>            Override the generated topology DB UUID
  --ansible-user ubuntu          Override the SSH user embedded in the fragment
  --inventory infra/ansible/inventory/multi-site.yml
  --playbook infra/ansible/playbooks/add-site.yml
  --keep-fragment                Keep an existing generated fragment instead of overwriting it

Environment:
  STEP_CA_URL
  STEP_CA_FINGERPRINT
  STEP_PROVISIONER
  STEP_PROVISIONER_PASSWORD_FILE
EOF
}

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
INVENTORY_BASE="$REPO_ROOT/infra/ansible/inventory/multi-site.yml"
PLAYBOOK_PATH="$REPO_ROOT/infra/ansible/playbooks/add-site.yml"
FRAGMENT_DIR="$REPO_ROOT/artifacts/site-onboarding"

SITE_ID=""
SITE_NAME=""
EDGE_HOST=""
NATS_HOST=""
CAMERA_COUNT=""
DB_SITE_ID=""
ANSIBLE_USER="ubuntu"
KEEP_FRAGMENT=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --site-id)
      SITE_ID="$2"
      shift 2
      ;;
    --site-name)
      SITE_NAME="$2"
      shift 2
      ;;
    --edge-host)
      EDGE_HOST="$2"
      shift 2
      ;;
    --nats-host)
      NATS_HOST="$2"
      shift 2
      ;;
    --camera-count)
      CAMERA_COUNT="$2"
      shift 2
      ;;
    --db-site-id)
      DB_SITE_ID="$2"
      shift 2
      ;;
    --ansible-user)
      ANSIBLE_USER="$2"
      shift 2
      ;;
    --inventory)
      INVENTORY_BASE="$2"
      shift 2
      ;;
    --playbook)
      PLAYBOOK_PATH="$2"
      shift 2
      ;;
    --keep-fragment)
      KEEP_FRAGMENT=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      printf 'Unknown argument: %s\n' "$1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

[[ -n "$SITE_ID" ]] || { printf 'Missing --site-id\n' >&2; exit 1; }
[[ -n "$SITE_NAME" ]] || { printf 'Missing --site-name\n' >&2; exit 1; }
[[ -n "$EDGE_HOST" ]] || { printf 'Missing --edge-host\n' >&2; exit 1; }
[[ -n "$CAMERA_COUNT" ]] || { printf 'Missing --camera-count\n' >&2; exit 1; }
[[ "$CAMERA_COUNT" =~ ^[0-9]+$ ]] || { printf '--camera-count must be numeric\n' >&2; exit 1; }
[[ -f "$INVENTORY_BASE" ]] || { printf 'Inventory not found: %s\n' "$INVENTORY_BASE" >&2; exit 1; }
[[ -f "$PLAYBOOK_PATH" ]] || { printf 'Playbook not found: %s\n' "$PLAYBOOK_PATH" >&2; exit 1; }
command -v ansible-playbook >/dev/null 2>&1 || { printf 'ansible-playbook is required\n' >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { printf 'python3 is required\n' >&2; exit 1; }

[[ -n "${STEP_CA_URL:-}" ]] || { printf 'STEP_CA_URL must be set\n' >&2; exit 1; }
[[ -n "${STEP_CA_FINGERPRINT:-}" ]] || { printf 'STEP_CA_FINGERPRINT must be set\n' >&2; exit 1; }

if [[ -z "$NATS_HOST" ]]; then
  NATS_HOST="$EDGE_HOST"
fi

if [[ -z "$DB_SITE_ID" ]]; then
  DB_SITE_ID="$(python3 - <<'PY'
import uuid
print(uuid.uuid4())
PY
)"
fi

SITE_GROUP="site_${SITE_ID//-/_}"
EDGE_INVENTORY_HOST="edge-${SITE_ID}.cilex.internal"
NATS_INVENTORY_HOST="nats-${SITE_ID}.edge.cilex.internal"
SITE_PKI_OUTPUT_DIR="$REPO_ROOT/artifacts/pki/$SITE_ID"
FRAGMENT_PATH="$FRAGMENT_DIR/${SITE_GROUP}.yml"

mkdir -p "$FRAGMENT_DIR" "$SITE_PKI_OUTPUT_DIR"

if [[ "$KEEP_FRAGMENT" -eq 0 || ! -f "$FRAGMENT_PATH" ]]; then
  {
    cat <<EOF
---
all:
  vars:
    ansible_user: ${ANSIBLE_USER}
    ansible_become: true
  children:
    sites:
      children:
        ${SITE_GROUP}:
          vars:
            site_id: ${SITE_ID}
            site_db_id: "${DB_SITE_ID}"
            site_name: "${SITE_NAME}"
            camera_count: ${CAMERA_COUNT}
            site_pki_output_dir: "${SITE_PKI_OUTPUT_DIR}"
            site_kafka_scram_user: "svc-ingress-bridge-${SITE_ID}"
            site_cameras:
EOF
    for index in $(seq 1 "$CAMERA_COUNT"); do
      camera_suffix="$(printf '%03d' "$index")"
      zone_suffix="$(printf '%02d' "$index")"
      cat <<EOF
              - camera_id: cam-${SITE_ID}-${camera_suffix}
                name: "Camera ${camera_suffix}"
                rtsp_url: rtsp://replace-me/${SITE_ID}/cam-${camera_suffix}
                zone_id: zone-${zone_suffix}
                location_description: "Replace with the physical placement for camera ${camera_suffix}"
                enabled: false
EOF
    done
    cat <<EOF
            site_clock_targets: []
          hosts:
            ${EDGE_INVENTORY_HOST}: {}
            ${NATS_INVENTORY_HOST}: {}
    edge_gateways:
      hosts:
        ${EDGE_INVENTORY_HOST}:
          ansible_host: ${EDGE_HOST}
          edge_site_id: ${SITE_ID}
          edge_nats_url: tls://${NATS_INVENTORY_HOST}:4222
    nats:
      hosts:
        ${NATS_INVENTORY_HOST}:
          ansible_host: ${NATS_HOST}
          nats_site_id: ${SITE_ID}
EOF
  } >"$FRAGMENT_PATH"
fi

SITE_ID="$SITE_ID" \
STEP_CA_URL="$STEP_CA_URL" \
STEP_CA_FINGERPRINT="$STEP_CA_FINGERPRINT" \
STEP_PROVISIONER="${STEP_PROVISIONER:-ops-bootstrap}" \
STEP_PROVISIONER_PASSWORD_FILE="${STEP_PROVISIONER_PASSWORD_FILE:-}" \
OUTPUT_DIR="$SITE_PKI_OUTPUT_DIR" \
bash "$REPO_ROOT/infra/pki/bootstrap-site.sh"

ansible-playbook \
  -i "$INVENTORY_BASE" \
  -i "$FRAGMENT_PATH" \
  "$PLAYBOOK_PATH" \
  -e "site_group=$SITE_GROUP"

cat <<EOF
Site onboarding complete.

Site summary:
  site_id:           ${SITE_ID}
  site_name:         ${SITE_NAME}
  site_group:        ${SITE_GROUP}
  site_db_id:        ${DB_SITE_ID}
  edge_host:         ${EDGE_HOST}
  nats_host:         ${NATS_HOST}
  camera_count:      ${CAMERA_COUNT}
  inventory_fragment:${FRAGMENT_PATH}
  pki_bundle:        ${SITE_PKI_OUTPUT_DIR}

Next steps:
  1. Replace the placeholder RTSP URLs in ${FRAGMENT_PATH}.
  2. Set the relevant cameras to enabled: true once credentials and network reachability are verified.
  3. Add any real site clock targets before re-running deploy-monitoring or add-site.
EOF
