#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
MinIO bucket backup via mc mirror.

Usage:
  backup-minio.sh [--target-alias backup] [--buckets "frame-blobs,event-clips,thumbnails"]

Options:
  --target-alias NAME    Alias used for the backup destination. Default: backup
  --source-alias NAME    Alias used for the source MinIO endpoint. Default: local
  --buckets CSV          Comma-separated bucket list to mirror
  -h, --help             Show this help

Environment:
  MINIO_ALIAS            Default source alias (local)
  BACKUP_ALIAS           Default target alias (backup)
  MINIO_ENDPOINT         Source endpoint. Default: http://localhost:9000
  MINIO_ACCESS_KEY       Source access key. Default: minioadmin
  MINIO_SECRET_KEY       Source secret key. Default: minioadmin123
  BACKUP_ENDPOINT        Backup endpoint (required unless aliases are preconfigured in local mc)
  BACKUP_ACCESS_KEY      Backup access key
  BACKUP_SECRET_KEY      Backup secret key
  MC_IMAGE               Default: minio/mc:RELEASE.2024-06-11T21-32-12Z
EOF
}

DEFAULT_BUCKETS="frame-blobs,decoded-frames,event-clips,thumbnails,debug-traces,raw-video,archive-warm,mtmc-checkpoints"
MINIO_ALIAS="${MINIO_ALIAS:-local}"
BACKUP_ALIAS="${BACKUP_ALIAS:-backup}"
MINIO_ENDPOINT="${MINIO_ENDPOINT:-http://localhost:9000}"
MINIO_ACCESS_KEY="${MINIO_ACCESS_KEY:-minioadmin}"
MINIO_SECRET_KEY="${MINIO_SECRET_KEY:-minioadmin123}"
BACKUP_ENDPOINT="${BACKUP_ENDPOINT:-}"
BACKUP_ACCESS_KEY="${BACKUP_ACCESS_KEY:-}"
BACKUP_SECRET_KEY="${BACKUP_SECRET_KEY:-}"
BUCKETS_CSV="${BUCKETS:-$DEFAULT_BUCKETS}"
MC_IMAGE="${MC_IMAGE:-minio/mc:RELEASE.2024-06-11T21-32-12Z}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --target-alias)
      BACKUP_ALIAS="$2"
      shift 2
      ;;
    --source-alias)
      MINIO_ALIAS="$2"
      shift 2
      ;;
    --buckets)
      BUCKETS_CSV="$2"
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

command -v docker >/dev/null 2>&1 || {
  printf 'docker is required\n' >&2
  exit 1
}

MC_CONFIG_DIR="$(mktemp -d)"
cleanup() {
  rm -rf "$MC_CONFIG_DIR"
}
trap cleanup EXIT

run_mc() {
  docker run --rm --network host \
    -e MC_CONFIG_DIR=/root/.mc \
    -v "${MC_CONFIG_DIR}:/root/.mc" \
    "$MC_IMAGE" "$@"
}

run_mc alias set "$MINIO_ALIAS" "$MINIO_ENDPOINT" "$MINIO_ACCESS_KEY" "$MINIO_SECRET_KEY" >/dev/null

if [[ -n "$BACKUP_ENDPOINT" ]]; then
  run_mc alias set "$BACKUP_ALIAS" "$BACKUP_ENDPOINT" "$BACKUP_ACCESS_KEY" "$BACKUP_SECRET_KEY" >/dev/null
else
  printf 'BACKUP_ENDPOINT is required for docker-based mc execution\n' >&2
  exit 1
fi

mirrored_count=0
skipped_count=0
IFS=',' read -r -a buckets <<< "$BUCKETS_CSV"
for bucket in "${buckets[@]}"; do
  bucket="${bucket// /}"
  [[ -n "$bucket" ]] || continue
  if ! run_mc ls "${MINIO_ALIAS}/${bucket}" >/dev/null 2>&1; then
    printf 'WARN: bucket not found, skipping: %s\n' "$bucket" >&2
    skipped_count=$((skipped_count + 1))
    continue
  fi
  printf 'Mirroring bucket %s...\n' "$bucket"
  run_mc mirror --overwrite --remove "${MINIO_ALIAS}/${bucket}" "${BACKUP_ALIAS}/${bucket}"
  mirrored_count=$((mirrored_count + 1))
done

printf 'MinIO backup complete.\n'
printf '  mirrored buckets: %d\n' "$mirrored_count"
printf '  skipped buckets:  %d\n' "$skipped_count"

