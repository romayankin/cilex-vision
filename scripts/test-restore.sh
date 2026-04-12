#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Test restore procedure in a temporary TimescaleDB container.

Usage:
  test-restore.sh --db-backup /backups/postgres/latest.dump

Options:
  --db-backup PATH       Required pg_dump custom-format backup
  --image IMAGE          TimescaleDB image. Default: timescale/timescaledb:2.18.1-pg16
  --container NAME       Temporary container name
  -h, --help             Show this help

Environment:
  POSTGRES_USER          Default: cilex
  POSTGRES_PASSWORD      Default: cilex_dev_password
  POSTGRES_DB            Default: vidanalytics
EOF
}

DB_BACKUP=""
IMAGE_NAME="${IMAGE_NAME:-timescale/timescaledb:2.18.1-pg16}"
CONTAINER_NAME="${CONTAINER_NAME:-cilex-test-restore-$$}"
POSTGRES_USER="${POSTGRES_USER:-cilex}"
POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-cilex_dev_password}"
POSTGRES_DB="${POSTGRES_DB:-vidanalytics}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --db-backup)
      DB_BACKUP="$2"
      shift 2
      ;;
    --image)
      IMAGE_NAME="$2"
      shift 2
      ;;
    --container)
      CONTAINER_NAME="$2"
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

[[ -n "$DB_BACKUP" ]] || {
  printf 'Missing --db-backup\n' >&2
  exit 1
}
[[ -f "$DB_BACKUP" ]] || {
  printf 'Backup file not found: %s\n' "$DB_BACKUP" >&2
  exit 1
}

command -v docker >/dev/null 2>&1 || {
  printf 'docker is required\n' >&2
  exit 1
}

backup_dir="$(cd "$(dirname "$DB_BACKUP")" && pwd)"
backup_name="$(basename "$DB_BACKUP")"

cleanup() {
  docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker run -d --rm \
  --name "$CONTAINER_NAME" \
  -e POSTGRES_USER="$POSTGRES_USER" \
  -e POSTGRES_PASSWORD="$POSTGRES_PASSWORD" \
  -e POSTGRES_DB="$POSTGRES_DB" \
  -v "${backup_dir}:/backups:ro" \
  "$IMAGE_NAME" >/dev/null

for _ in $(seq 1 60); do
  if docker exec "$CONTAINER_NAME" pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB" >/dev/null 2>&1; then
    break
  fi
  sleep 2
done

docker exec "$CONTAINER_NAME" pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB" >/dev/null 2>&1 || {
  printf 'Temporary restore database did not become ready\n' >&2
  exit 1
}

docker exec "$CONTAINER_NAME" psql -U "$POSTGRES_USER" -d postgres -v ON_ERROR_STOP=1 -c \
  "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '${POSTGRES_DB}' AND pid <> pg_backend_pid();" \
  >/dev/null

docker exec "$CONTAINER_NAME" dropdb -U "$POSTGRES_USER" --if-exists "$POSTGRES_DB"
docker exec "$CONTAINER_NAME" createdb -U "$POSTGRES_USER" "$POSTGRES_DB"
docker exec -i "$CONTAINER_NAME" pg_restore \
  -U "$POSTGRES_USER" \
  -d "$POSTGRES_DB" \
  --clean \
  --if-exists \
  --no-owner \
  --no-privileges \
  "/backups/${backup_name}" >/dev/null

check_table() {
  local table_name="$1"
  docker exec "$CONTAINER_NAME" psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atqc \
    "SELECT to_regclass('public.${table_name}') IS NOT NULL;" | grep -qx "t"
}

count_table() {
  local table_name="$1"
  docker exec "$CONTAINER_NAME" psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atqc \
    "SELECT COUNT(*) FROM ${table_name};"
}

for table_name in sites cameras detections local_tracks events; do
  check_table "$table_name" || {
    printf 'Restore test failed: missing table %s\n' "$table_name" >&2
    exit 1
  }
done

printf 'Restore test passed.\n'
for table_name in sites cameras detections local_tracks events; do
  printf '  %s: %s\n' "$table_name" "$(count_table "$table_name")"
done

