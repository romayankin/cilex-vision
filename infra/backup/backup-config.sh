#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Configuration backup for Ansible, Terraform, PKI, env files, and MTMC checkpoints.

Usage:
  backup-config.sh [--output-dir /backups/config] [--repo-root /repo]

Options:
  --output-dir PATH      Destination directory. Default: backups/config under repo root
  --repo-root PATH       Repository root. Default: auto-detected from this script
  --retention-count N    Keep the newest N archives. Default: 30
  -h, --help             Show this help

Environment:
  MINIO_ENDPOINT         Default: http://localhost:9000
  MINIO_ACCESS_KEY       Default: minioadmin
  MINIO_SECRET_KEY       Default: minioadmin123
  MINIO_ALIAS            Default: local
  MC_IMAGE               Default: minio/mc:RELEASE.2024-06-11T21-32-12Z
EOF
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT_DEFAULT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

REPO_ROOT="$REPO_ROOT_DEFAULT"
OUTPUT_DIR="${OUTPUT_DIR:-${REPO_ROOT_DEFAULT}/backups/config}"
RETENTION_COUNT="${RETENTION_COUNT:-30}"
MINIO_ENDPOINT="${MINIO_ENDPOINT:-http://localhost:9000}"
MINIO_ACCESS_KEY="${MINIO_ACCESS_KEY:-minioadmin}"
MINIO_SECRET_KEY="${MINIO_SECRET_KEY:-minioadmin123}"
MINIO_ALIAS="${MINIO_ALIAS:-local}"
MC_IMAGE="${MC_IMAGE:-minio/mc:RELEASE.2024-06-11T21-32-12Z}"
MTMC_BUCKET="${MTMC_BUCKET:-mtmc-checkpoints}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --output-dir)
      OUTPUT_DIR="$2"
      shift 2
      ;;
    --repo-root)
      REPO_ROOT="$2"
      shift 2
      ;;
    --retention-count)
      RETENTION_COUNT="$2"
      shift 2
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

[[ "$RETENTION_COUNT" =~ ^[0-9]+$ ]] || {
  printf 'RETENTION_COUNT must be a non-negative integer\n' >&2
  exit 1
}

command -v docker >/dev/null 2>&1 || {
  printf 'docker is required\n' >&2
  exit 1
}

[[ -d "$REPO_ROOT" ]] || {
  printf 'Repository root not found: %s\n' "$REPO_ROOT" >&2
  exit 1
}

mkdir -p "$OUTPUT_DIR"
timestamp="$(date -u +%Y%m%d-%H%M%S)"
archive_path="${OUTPUT_DIR}/cilex-config-${timestamp}.tar.gz"
staging_dir="$(mktemp -d)"
mc_config_dir="$(mktemp -d)"

cleanup() {
  rm -rf "$staging_dir" "$mc_config_dir"
}
trap cleanup EXIT

copy_rel_path() {
  local relative_path="$1"
  local source_path="${REPO_ROOT}/${relative_path}"
  local dest_dir="${staging_dir}/repo/$(dirname "$relative_path")"
  mkdir -p "$dest_dir"
  cp -a "$source_path" "$dest_dir/"
}

copy_rel_path "infra/ansible"
copy_rel_path "infra/pki"
copy_rel_path "infra/terraform"

while IFS= read -r -d '' env_file; do
  relative_path="${env_file#"${REPO_ROOT}/"}"
  copy_rel_path "$relative_path"
done < <(
  find "$REPO_ROOT" -type f \
    \( -name '.env' -o -name '.env.*' \) \
    ! -path '*/.venv/*' \
    ! -path '*/node_modules/*' \
    -print0
)

run_mc() {
  docker run --rm --network host \
    -e MC_CONFIG_DIR=/root/.mc \
    -v "${mc_config_dir}:/root/.mc" \
    -v "${staging_dir}:${staging_dir}" \
    "$MC_IMAGE" "$@"
}

run_mc alias set "$MINIO_ALIAS" "$MINIO_ENDPOINT" "$MINIO_ACCESS_KEY" "$MINIO_SECRET_KEY" >/dev/null
if run_mc ls "${MINIO_ALIAS}/${MTMC_BUCKET}" >/dev/null 2>&1; then
  mkdir -p "${staging_dir}/mtmc-checkpoints"
  run_mc mirror --overwrite "${MINIO_ALIAS}/${MTMC_BUCKET}" "${staging_dir}/mtmc-checkpoints" >/dev/null
else
  printf 'WARN: MinIO bucket not found, skipping checkpoint snapshot: %s\n' "$MTMC_BUCKET" >&2
fi

tar -C "$staging_dir" -czf "$archive_path" .

mapfile -t archives < <(
  find "$OUTPUT_DIR" -maxdepth 1 -type f -name 'cilex-config-*.tar.gz' | sort
)
if (( ${#archives[@]} > RETENTION_COUNT )); then
  delete_count=$(( ${#archives[@]} - RETENTION_COUNT ))
  for archive in "${archives[@]:0:delete_count}"; do
    rm -f "$archive"
  done
fi

printf 'Config backup complete.\n'
printf '  archive: %s\n' "$archive_path"
printf '  retained archives: %s\n' "$RETENTION_COUNT"

