#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Complete system restore from configuration, database, and MinIO backups.

Usage:
  restore-full.sh --db-backup /backups/postgres/latest.dump \
      --config-backup /backups/config/latest.tar.gz

Options:
  --db-backup PATH           Required pg_dump custom-format backup
  --globals-file PATH        Optional pg_dumpall --globals-only SQL file
  --config-backup PATH       Required config archive from backup-config.sh
  --restore-root PATH        Restore destination for repo config. Default: repo root
  --container NAME           Database container name. Default: timescaledb
  --buckets CSV              MinIO buckets to restore. Default: all critical buckets
  --skip-minio               Skip MinIO bucket restore
  -h, --help                 Show this help

Environment:
  MINIO_ENDPOINT             Destination MinIO endpoint. Default: http://localhost:9000
  MINIO_ACCESS_KEY           Destination access key. Default: minioadmin
  MINIO_SECRET_KEY           Destination secret key. Default: minioadmin123
  BACKUP_ENDPOINT            Source MinIO backup endpoint
  BACKUP_ACCESS_KEY          Source MinIO backup access key
  BACKUP_SECRET_KEY          Source MinIO backup secret key
  BACKUP_ALIAS               Source alias name. Default: backup
  MINIO_ALIAS                Destination alias name. Default: local
EOF
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
HEALTH_CHECK_SCRIPT="${REPO_ROOT}/scripts/deploy/health-check-all.sh"
RESTORE_DB_SCRIPT="${SCRIPT_DIR}/restore-db.sh"

DB_BACKUP=""
GLOBALS_FILE=""
CONFIG_BACKUP=""
RESTORE_ROOT="$REPO_ROOT"
CONTAINER_NAME="${CONTAINER_NAME:-timescaledb}"
POSTGRES_USER="${POSTGRES_USER:-cilex}"
POSTGRES_DB="${POSTGRES_DB:-vidanalytics}"
MINIO_ALIAS="${MINIO_ALIAS:-local}"
BACKUP_ALIAS="${BACKUP_ALIAS:-backup}"
MINIO_ENDPOINT="${MINIO_ENDPOINT:-http://localhost:9000}"
MINIO_ACCESS_KEY="${MINIO_ACCESS_KEY:-minioadmin}"
MINIO_SECRET_KEY="${MINIO_SECRET_KEY:-minioadmin123}"
BACKUP_ENDPOINT="${BACKUP_ENDPOINT:-}"
BACKUP_ACCESS_KEY="${BACKUP_ACCESS_KEY:-}"
BACKUP_SECRET_KEY="${BACKUP_SECRET_KEY:-}"
MC_IMAGE="${MC_IMAGE:-minio/mc:RELEASE.2024-06-11T21-32-12Z}"
BUCKETS_CSV="${BUCKETS:-frame-blobs,decoded-frames,event-clips,thumbnails,debug-traces,raw-video,archive-warm,mtmc-checkpoints}"
SKIP_MINIO=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --db-backup)
      DB_BACKUP="$2"
      shift 2
      ;;
    --globals-file)
      GLOBALS_FILE="$2"
      shift 2
      ;;
    --config-backup)
      CONFIG_BACKUP="$2"
      shift 2
      ;;
    --restore-root)
      RESTORE_ROOT="$2"
      shift 2
      ;;
    --container)
      CONTAINER_NAME="$2"
      shift 2
      ;;
    --buckets)
      BUCKETS_CSV="$2"
      shift 2
      ;;
    --skip-minio)
      SKIP_MINIO=1
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

[[ -n "$DB_BACKUP" ]] || {
  printf 'Missing --db-backup\n' >&2
  exit 1
}
[[ -n "$CONFIG_BACKUP" ]] || {
  printf 'Missing --config-backup\n' >&2
  exit 1
}
[[ -f "$DB_BACKUP" ]] || {
  printf 'DB backup not found: %s\n' "$DB_BACKUP" >&2
  exit 1
}
[[ -f "$CONFIG_BACKUP" ]] || {
  printf 'Config backup not found: %s\n' "$CONFIG_BACKUP" >&2
  exit 1
}
if [[ -n "$GLOBALS_FILE" && ! -f "$GLOBALS_FILE" ]]; then
  printf 'Globals file not found: %s\n' "$GLOBALS_FILE" >&2
  exit 1
fi

command -v docker >/dev/null 2>&1 || {
  printf 'docker is required\n' >&2
  exit 1
}
[[ -x "$RESTORE_DB_SCRIPT" || -f "$RESTORE_DB_SCRIPT" ]] || {
  printf 'restore-db.sh not found: %s\n' "$RESTORE_DB_SCRIPT" >&2
  exit 1
}
[[ -x "$HEALTH_CHECK_SCRIPT" || -f "$HEALTH_CHECK_SCRIPT" ]] || {
  printf 'health-check-all.sh not found: %s\n' "$HEALTH_CHECK_SCRIPT" >&2
  exit 1
}

start_epoch="$(date +%s)"
extract_dir="$(mktemp -d)"
mc_config_dir="$(mktemp -d)"

cleanup() {
  rm -rf "$extract_dir" "$mc_config_dir"
}
trap cleanup EXIT

printf 'Extracting config archive...\n'
mkdir -p "$RESTORE_ROOT"
tar -xzf "$CONFIG_BACKUP" -C "$extract_dir"

if [[ -d "${extract_dir}/repo" ]]; then
  cp -a "${extract_dir}/repo/." "$RESTORE_ROOT"/
fi

printf 'Restoring database...\n'
db_cmd=(bash "$RESTORE_DB_SCRIPT" --backup-file "$DB_BACKUP" --container "$CONTAINER_NAME")
if [[ -n "$GLOBALS_FILE" ]]; then
  db_cmd+=(--globals-file "$GLOBALS_FILE")
fi
POSTGRES_USER="$POSTGRES_USER" POSTGRES_DB="$POSTGRES_DB" CONTAINER_NAME="$CONTAINER_NAME" "${db_cmd[@]}"

run_mc() {
  docker run --rm --network host \
    -e MC_CONFIG_DIR=/root/.mc \
    -v "${mc_config_dir}:/root/.mc" \
    -v "${extract_dir}:${extract_dir}" \
    "$MC_IMAGE" "$@"
}

if [[ "$SKIP_MINIO" -eq 0 ]]; then
  [[ -n "$BACKUP_ENDPOINT" ]] || {
    printf 'BACKUP_ENDPOINT is required unless --skip-minio is used\n' >&2
    exit 1
  }
  printf 'Restoring MinIO buckets...\n'
  run_mc alias set "$MINIO_ALIAS" "$MINIO_ENDPOINT" "$MINIO_ACCESS_KEY" "$MINIO_SECRET_KEY" >/dev/null
  run_mc alias set "$BACKUP_ALIAS" "$BACKUP_ENDPOINT" "$BACKUP_ACCESS_KEY" "$BACKUP_SECRET_KEY" >/dev/null

  IFS=',' read -r -a buckets <<< "$BUCKETS_CSV"
  for bucket in "${buckets[@]}"; do
    bucket="${bucket// /}"
    [[ -n "$bucket" ]] || continue
    if ! run_mc ls "${BACKUP_ALIAS}/${bucket}" >/dev/null 2>&1; then
      printf 'WARN: backup bucket missing, skipping: %s\n' "$bucket" >&2
      continue
    fi
    run_mc mirror --overwrite --remove "${BACKUP_ALIAS}/${bucket}" "${MINIO_ALIAS}/${bucket}"
  done
fi

if [[ -d "${extract_dir}/mtmc-checkpoints" ]]; then
  printf 'Restoring MTMC checkpoints from config archive snapshot...\n'
  run_mc alias set "$MINIO_ALIAS" "$MINIO_ENDPOINT" "$MINIO_ACCESS_KEY" "$MINIO_SECRET_KEY" >/dev/null
  run_mc mirror --overwrite --remove "${extract_dir}/mtmc-checkpoints" "${MINIO_ALIAS}/mtmc-checkpoints"
fi

printf 'Running health checks...\n'
bash "$HEALTH_CHECK_SCRIPT" --no-color

finish_epoch="$(date +%s)"
elapsed_seconds=$((finish_epoch - start_epoch))

age_minutes() {
  local file_path="$1"
  local now_epoch
  local file_epoch
  now_epoch="$(date +%s)"
  file_epoch="$(stat -c %Y "$file_path")"
  printf '%d' $(((now_epoch - file_epoch) / 60))
}

db_rpo_minutes="$(age_minutes "$DB_BACKUP")"
config_rpo_minutes="$(age_minutes "$CONFIG_BACKUP")"

printf 'Restore summary:\n'
printf '  restore_root:           %s\n' "$RESTORE_ROOT"
printf '  db_backup:              %s\n' "$DB_BACKUP"
printf '  db_backup_age_minutes:  %s (target <= 15)\n' "$db_rpo_minutes"
printf '  config_backup:          %s\n' "$CONFIG_BACKUP"
printf '  config_backup_age_min:  %s (target <= 1440)\n' "$config_rpo_minutes"
printf '  elapsed_seconds:        %s\n' "$elapsed_seconds"
printf '  db_rto_target_seconds:  7200\n'
printf '  config_rto_target_sec:  3600\n'
printf '  minio_restore:          %s\n' "$([[ "$SKIP_MINIO" -eq 0 ]] && printf 'performed' || printf 'skipped')"

