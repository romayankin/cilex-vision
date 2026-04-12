#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
TimescaleDB/PostgreSQL full restore from a pg_dump custom-format backup.

Usage:
  restore-db.sh --backup-file /backups/postgres/vidanalytics-20260410.dump

Options:
  --backup-file PATH     Required pg_dump custom-format backup
  --globals-file PATH    Optional pg_dumpall --globals-only SQL file
  --container NAME       Database container name. Default: $CONTAINER_NAME or timescaledb
  -h, --help             Show this help

Environment:
  POSTGRES_USER          Default: cilex
  POSTGRES_DB            Default: vidanalytics
  CONTAINER_NAME         Default: timescaledb
EOF
}

BACKUP_FILE=""
GLOBALS_FILE=""
CONTAINER_NAME="${CONTAINER_NAME:-timescaledb}"
POSTGRES_USER="${POSTGRES_USER:-cilex}"
POSTGRES_DB="${POSTGRES_DB:-vidanalytics}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --backup-file)
      BACKUP_FILE="$2"
      shift 2
      ;;
    --globals-file)
      GLOBALS_FILE="$2"
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

[[ -n "$BACKUP_FILE" ]] || {
  printf 'Missing --backup-file\n' >&2
  exit 1
}
[[ -f "$BACKUP_FILE" ]] || {
  printf 'Backup file not found: %s\n' "$BACKUP_FILE" >&2
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

docker inspect "$CONTAINER_NAME" >/dev/null 2>&1 || {
  printf 'Container not found: %s\n' "$CONTAINER_NAME" >&2
  exit 1
}

container_running="$(docker inspect --format='{{.State.Running}}' "$CONTAINER_NAME" 2>/dev/null || true)"
[[ "$container_running" == "true" ]] || {
  printf 'Container is not running: %s\n' "$CONTAINER_NAME" >&2
  exit 1
}

docker exec "$CONTAINER_NAME" pg_isready -U "$POSTGRES_USER" -d postgres >/dev/null

printf 'Restoring database %s in container %s...\n' "$POSTGRES_DB" "$CONTAINER_NAME"

docker exec "$CONTAINER_NAME" psql -U "$POSTGRES_USER" -d postgres -v ON_ERROR_STOP=1 -c \
  "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '${POSTGRES_DB}' AND pid <> pg_backend_pid();" \
  >/dev/null

docker exec "$CONTAINER_NAME" dropdb -U "$POSTGRES_USER" --if-exists "$POSTGRES_DB"
docker exec "$CONTAINER_NAME" createdb -U "$POSTGRES_USER" "$POSTGRES_DB"

if [[ -n "$GLOBALS_FILE" ]]; then
  cat "$GLOBALS_FILE" | docker exec -i "$CONTAINER_NAME" psql -U "$POSTGRES_USER" -d postgres -v ON_ERROR_STOP=1 >/dev/null
fi

cat "$BACKUP_FILE" | docker exec -i "$CONTAINER_NAME" pg_restore \
  -U "$POSTGRES_USER" \
  -d "$POSTGRES_DB" \
  --clean \
  --if-exists \
  --no-owner \
  --no-privileges

verify_table() {
  local table_name="$1"
  docker exec "$CONTAINER_NAME" psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atqc \
    "SELECT to_regclass('public.${table_name}') IS NOT NULL;" | grep -qx "t"
}

table_count() {
  local table_name="$1"
  docker exec "$CONTAINER_NAME" psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atqc \
    "SELECT COUNT(*) FROM ${table_name};"
}

for table_name in sites cameras detections local_tracks events; do
  verify_table "$table_name" || {
    printf 'Restore verification failed: missing table %s\n' "$table_name" >&2
    exit 1
  }
done

printf 'Restore verification counts:\n'
for table_name in sites cameras detections local_tracks events; do
  printf '  %s: %s\n' "$table_name" "$(table_count "$table_name")"
done

printf 'Database restore complete.\n'

